"""Each session gets its own browser: its own approved sites, its own page, its own close.

Runs without Chromium: `web._Browser` is replaced by a fake that records what it was asked to do, so the
isolation rules (one browser per session, one set of approvals each, closing one leaves the other) are
tested directly. The real browser (Playwright, Chromium) is exercised by tests/test_web.py.
"""

from __future__ import annotations

import threading
import time

import pytest

from seedagent import state
from seedagent.common import ToolError
from seedagent.state import Session
from seedagent.tools import web


class FakeBrowser(web._Browser):
    """Stands in for _Browser: real slot handling and close(), but no Playwright / Chromium."""

    made = 0

    def __init__(self):
        super().__init__()          # real _tasks/_lock/all_sites/_slot/last_used
        FakeBrowser.made += 1
        self.this = FakeBrowser.made
        self.closed = 0

    def call(self, fn, timeout: int = 120):
        return fn(self)

    def ensure_page(self):
        return self                # a fake "page": the tools only pass it around

    def close(self):
        self.closed += 1
        self._release_slot()
        return True


@pytest.fixture
def fake(monkeypatch):
    """web._Browser builds FakeBrowsers; the slot semaphore starts roomy unless a test shrinks it."""
    FakeBrowser.made = 0
    monkeypatch.setattr(web, "_Browser", FakeBrowser)
    monkeypatch.setattr(web, "_BROWSER_SLOTS", threading.BoundedSemaphore(10))
    monkeypatch.setattr(web, "WEB_IDLE_SECONDS", 0)  # no idle closing unless a test asks for it
    return FakeBrowser


@pytest.fixture
def two(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    return Session((tmp_path / "a").resolve()), Session((tmp_path / "b").resolve())


def test_each_session_gets_its_own_browser(two, fake):
    a, b = two
    with state.activate(a):
        ba = web._browser()
        assert web._browser() is ba              # the same session keeps one browser
    with state.activate(b):
        bb = web._browser()
        assert bb is not ba                      # another session gets another one
    assert a.browser is ba and b.browser is bb


def test_one_session_keeps_one_browser_under_two_threads(two, fake):
    """Two threads of the *same* session calling at once must get the same browser (the module lock)."""
    a, _ = two
    got, barrier = [], threading.Barrier(2)

    def worker():
        with state.activate(a):
            barrier.wait()
            got.append(web._browser())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 2 and got[0] is got[1] and FakeBrowser.made == 1


def test_approving_a_site_in_one_session_does_not_approve_it_in_the_other(two, fake, monkeypatch):
    monkeypatch.setattr(web, "WEB_APPROVE", True)
    a, b = two
    with state.activate(a):
        ba = web._browser()
        ba.approved.add("example.com")
    with state.activate(b):
        bb = web._browser()
        assert bb.approved == set()              # b knows nothing of a's approval
        assert not web._approved("example.com", bb.all_sites, bb.approved)
    assert web._approved("example.com", ba.all_sites, ba.approved)


def test_all_sites_in_one_session_leaves_the_other_at_false(two, fake, monkeypatch):
    monkeypatch.setattr(web, "WEB_APPROVE", True)
    a, b = two
    with state.activate(a):
        web._browser().all_sites = True
    with state.activate(b):
        bb = web._browser()
        assert bb.all_sites is False
        assert not web._approved("anything.example", bb.all_sites, bb.approved)


def test_closing_one_session_leaves_the_other_browser_open(two, fake):
    a, b = two
    with state.activate(a):
        ba = web._browser()
    with state.activate(b):
        bb = web._browser()
    with state.activate(a):
        assert "closed" in web.tool_web_close()
        assert ba.closed == 1
    assert a.browser is None
    assert b.browser is bb and bb.closed == 0    # untouched
    with state.activate(b):
        assert web._browser() is bb              # b still has its browser


def test_a_session_without_a_browser_says_so(two, fake):
    a, _ = two
    with state.activate(a):
        assert web.tool_web_close() == "No browser was open."


def test_close_session_closes_only_that_session(two, fake):
    a, b = two
    with state.activate(a):
        ba = web._browser()
    with state.activate(b):
        bb = web._browser()
    state.close_session(a)
    assert a.browser is None and ba.closed == 1
    assert b.browser is bb and bb.closed == 0


def test_closing_twice_releases_the_slot_once(two, fake):
    a, _ = two
    with state.activate(a):
        ba = web._browser()
    ba._take_slot()
    assert ba._slot is not None
    ba.close()
    assert ba._slot is None
    ba.close()                                   # idempotent: must not raise (a BoundedSemaphore would)
    assert ba._slot is None


def test_a_second_session_cannot_take_a_slot_already_held(two, fake, monkeypatch):
    """With one slot and a holding another, b's _take_slot fails clearly instead of blocking for ever."""
    a, b = two
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(web, "_BROWSER_SLOTS", slots)
    monkeypatch.setattr(slots, "acquire", lambda timeout=None: False)  # as if the 30 s wait elapsed
    with state.activate(a):
        ba = web._browser()
        ba._slot = slots                             # a holds the one slot
    with state.activate(b):
        bb = web._browser()
        with pytest.raises(ToolError, match="Too many browsers"):
            bb._take_slot()
        assert bb._slot is None                      # and b kept nothing


def test_take_slot_reports_the_limit(two, fake, monkeypatch):
    """_take_slot itself: no slot free within the wait -> a clear ToolError, and no slot is kept."""
    a, _ = two
    with state.activate(a):
        b = web._browser()
    slots = threading.BoundedSemaphore(0)
    monkeypatch.setattr(web, "_BROWSER_SLOTS", slots)
    monkeypatch.setattr(slots, "acquire", lambda timeout=None: False)  # as if the wait elapsed
    with pytest.raises(ToolError, match="Too many browsers"):
        b._take_slot()
    assert b._slot is None


def test_the_slot_is_released_when_the_chromium_launch_fails(two, fake, monkeypatch):
    """A failed launch must never keep a slot (else the limit would leak)."""
    a, _ = two
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(web, "_BROWSER_SLOTS", slots)
    with state.activate(a):
        b = web._browser()
    b._take_slot()
    assert slots.acquire(blocking=False) is False   # the slot is held
    b._release_slot()
    assert slots.acquire(blocking=False) is True    # and freed
    slots.release()


def test_a_slot_is_released_to_the_semaphore_that_gave_it(two, fake, monkeypatch):
    """The bug this guards: _BROWSER_SLOTS is looked up again at release time, so a browser whose slot came
    from one semaphore would release another (a swapped/reloaded one) and corrupt its count."""
    a, _ = two
    with state.activate(a):
        b = web._browser()
    b._take_slot()                              # takes from the fixture's roomy semaphore
    other = threading.BoundedSemaphore(2)       # as if _BROWSER_SLOTS were swapped after the take
    monkeypatch.setattr(web, "_BROWSER_SLOTS", other)
    b._release_slot()
    assert other._value == 2                    # the other semaphore is untouched
    b._release_slot()                           # and releasing twice does nothing more
    assert other._value == 2


def test_an_idle_browser_is_closed_but_keeps_its_approved_sites(two, fake, monkeypatch):
    a, _ = two
    with state.activate(a):
        ba = web._browser()
        ba.approved.add("example.com")
    assert ba.closed == 0
    monkeypatch.setattr(web, "WEB_IDLE_SECONDS", 300)
    ba.last_used = time.monotonic() - 10_000         # as if nothing used it for a long while
    with state.activate(a):
        again = web._browser()
    assert ba.closed == 1 and again is ba            # closed, and the same object is reused
    assert web._approved("example.com", ba.all_sites, ba.approved)  # approvals kept across the idle close
