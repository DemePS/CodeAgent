"""Two sessions in one process: their workspaces, conversations, tools and approvals stay apart."""

from __future__ import annotations

import json
import threading

import httpx
import pytest
from anthropic import AnthropicFoundry

from seedagent import config, loop, memory, session, state
from seedagent.common import ToolError, resolve, resolve_readable
from seedagent.state import Session
from seedagent.ui import HeadlessUI, UI


@pytest.fixture
def two(tmp_path):
    """Two sessions, each with its own workspace folder."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    return Session((tmp_path / "a").resolve()), Session((tmp_path / "b").resolve())


def test_a_session_has_its_workspace(two):
    a, b = two
    assert a.cwd == a.workspace  # workspace and cwd follow the constructor
    assert a.workspace != b.workspace
    assert a.messages == [] and b.messages == []  # each session has its own conversation


def test_the_current_session_follows_activate(two):
    a, b = two
    state.set_current(a)
    assert state.current() is a
    with state.activate(b):
        assert state.current() is b
        assert state.workspace == b.workspace
    assert state.current() is a  # restored
    assert state.workspace == a.workspace


def test_the_module_reads_and_writes_the_current_session(two):
    a, b = two
    with state.activate(a):
        state.auto_mode = True
        state.cwd = a.workspace / "sub"
    assert a.auto_mode is True and a.cwd == a.workspace / "sub"
    assert b.auto_mode is False and b.cwd == b.workspace  # untouched


def test_paths_resolve_against_the_active_session(two):
    a, b = two
    (a.workspace / "x.txt").write_text("a")
    (b.workspace / "x.txt").write_text("b")
    with state.activate(a):
        assert resolve("x.txt").read_text() == "a"
    with state.activate(b):
        assert resolve("x.txt").read_text() == "b"


def test_a_session_cannot_reach_another_workspace(two):
    a, b = two
    with state.activate(a):
        with pytest.raises(ToolError, match="outside the workspace"):
            resolve(str(b.workspace / "x.txt"))
        with pytest.raises(ToolError, match="outside the workspace"):
            resolve_readable(str(b.workspace / "x.txt"))


def test_tool_names_are_per_session(two):
    a, b = two
    a.tool_names = {"read_file"}
    assert not a.tool_enabled("edit_file")
    assert b.tool_enabled("edit_file")  # b enables everything (None)


def test_two_threads_run_two_sessions(two):
    """The real point: two turns in two threads see their own workspace, cwd and UI at the same time."""
    a, b = two
    a.ui, b.ui = object(), object()
    seen = {}
    barrier = threading.Barrier(2)

    def turn(sess):
        with state.activate(sess):
            state.cwd = sess.workspace / "sub"
            barrier.wait()  # both turns are inside activate() at once: the state must not leak across
            seen[sess] = (state.cwd, state.ui, state.workspace)

    threads = [threading.Thread(target=turn, args=(s,)) for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen[a][0] == a.workspace / "sub" and seen[b][0] == b.workspace / "sub"
    assert seen[a][1] is a.ui and seen[b][1] is b.ui
    assert a.cwd == a.workspace / "sub" and b.cwd == b.workspace / "sub"


def test_for_messages_finds_the_owning_session(two):
    a, b = two
    assert state.for_messages(b.messages) is b
    assert state.for_messages(a.messages) is a
    state.set_current(a)
    assert state.for_messages([{"unknown": True}]) is a  # no owner: the current session


def test_stop_from_another_thread_reaches_the_running_session(two):
    """A Stop button (another thread, with no session of its own) must reach the session that is running."""
    a, b = two
    state.set_current(b)  # the caller's/main thread is on b
    with state.activate(a):  # a turn is running in a
        assert state.running() is a
        session.stop()  # called from anywhere: hits the running session, not b
        assert a.stop_requested is True
        assert b.stop_requested is False
    assert state.running() is state.current() == b  # outside a turn: the caller's own session


def test_session_messages_delegates_to_the_current_session(two):
    a, b = two
    with state.activate(a):
        session.messages.append({"role": "user", "content": "from a"})
    with state.activate(b):
        session.messages.append({"role": "user", "content": "from b"})
    assert [m["content"] for m in a.messages] == ["from a"]
    assert [m["content"] for m in b.messages] == ["from b"]


# --- open_project binds a session to the calling thread -------------------------------------------------

def test_open_project_binds_the_session_to_the_calling_thread(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    opened = session.open_project(project, ui=HeadlessUI())
    assert isinstance(opened, state.Session)
    assert state.bound() is opened  # this thread now drives the session it opened
    assert state.current() is opened


def test_another_thread_opening_a_project_does_not_steal_this_threads_session(tmp_path):
    """The bug this fixes: a thread that opened project a keeps driving a even when another thread opens
    another project -- open_project binds the *calling* thread (one thread runs one session at a time)."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    a = session.open_project(tmp_path / "a", ui=HeadlessUI(), session=Session(tmp_path / "a"))
    assert state.bound() is a

    seen = {}

    def worker():  # another thread opens project b: it must not change the main thread's session
        seen["worker"] = session.open_project(tmp_path / "b", ui=HeadlessUI(), session=Session(tmp_path / "b"))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert state.bound() is a  # untouched: the main thread still drives the project it opened
    assert seen["worker"].workspace.name == "b"  # the worker thread got its own session
    # and the main thread, opening its own second project, moves to it as expected
    b = session.open_project(tmp_path / "b", ui=HeadlessUI(), session=Session(tmp_path / "b"))
    assert state.bound() is b


def test_close_session_drops_the_thread_binding(tmp_path):
    (tmp_path / "p").mkdir()
    opened = session.open_project(tmp_path / "p", ui=HeadlessUI())
    assert state.bound() is opened
    state.close_session(opened)
    assert state.bound() is None


def test_for_messages_prefers_the_threads_bound_session(two):
    a, b = two
    state.bind(a)
    assert state.for_messages(a.messages) is a
    assert state.for_messages([{"unknown": True}]) is not b  # never silently the other session


# --- two real turns, end to end, against a mocked Claude (real SDK, fake HTTP) -----------------------

def _sse(blocks, stop):
    msg = {"id": "m", "type": "message", "role": "assistant", "model": "x", "content": [], "stop_reason": None,
           "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1,
                                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
    ev = [("message_start", {"type": "message_start", "message": msg})]
    for i, (kind, value) in enumerate(blocks):
        if kind == "text":
            ev += [("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": value}})]
        else:
            ev += [("content_block_start", {"type": "content_block_start", "index": i,
                                            "content_block": {"type": "tool_use", "id": f"toolu_{i}", "name": kind, "input": {}}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i,
                                            "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)}})]
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 1}}),
           ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in ev)


class _FakeClaude:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[dict] = []

    def handler(self, request):
        body = json.loads(request.content)
        with self.lock:
            self.requests.append(body)
        # every reply is one text block: no tools, so each turn is a single model call
        return httpx.Response(200, text=_sse([("text", "done")], "end_turn"), headers={"content-type": "text/event-stream"})


def test_a_whole_turn_runs_in_the_session_that_owns_it(tmp_path, monkeypatch):
    """A real turn (mock HTTP): the request carries the workspace and tool subset of the session that sent it,
    and each session keeps its own history -- even when the two turns run in two threads at once."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setattr(config, "MEMORY_UPDATES", False)
    monkeypatch.setattr(memory, "MEMORY_UPDATES", False)
    monkeypatch.setattr(config, "MEMORY_HOME", tmp_path / "mem")
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")  # conversations and memory under tmp, not ~/.coding-agent
    fake = _FakeClaude()
    client = AnthropicFoundry(api_key="k", base_url="https://x.services.ai.azure.com/anthropic",
                              http_client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(session, "_get_client", lambda: client)

    a, b = Session((tmp_path / "a").resolve()), Session((tmp_path / "b").resolve())
    session.open_project(a.workspace, ui=HeadlessUI(), tools=["read_file"], session=a)
    session.open_project(b.workspace, ui=HeadlessUI(), tools=["list_directory"], session=b)

    # run both turns in parallel; each thread makes its session current, exactly as a front end would
    def turn(sess, text):
        state.set_current(sess)
        assert session.send(text) is True

    threads = [threading.Thread(target=turn, args=(a, "hello from a")),
               threading.Thread(target=turn, args=(b, "hello from b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    def said(messages, needle):
        return sum(needle in json.dumps(m) for m in messages)

    assert said(a.messages, "hello from a") >= 1 and said(a.messages, "hello from b") == 0  # nothing leaked across
    assert said(b.messages, "hello from b") >= 1 and said(b.messages, "hello from a") == 0

    # each request offered the tool subset and the workspace of its own session
    seen = {}
    for request in fake.requests:
        body = json.dumps(request)
        key = "a" if "hello from a" in body else "b"
        system = request["system"] if isinstance(request["system"], str) else json.dumps(request["system"])
        seen[key] = ({t["name"] for t in request["tools"]}, system)
    assert seen["a"][0] == {"read_file"}
    assert seen["b"][0] == {"list_directory"}
    # the system prompt tells the agent which workspace it is in: the request carries its own, not the default's
    assert str(a.workspace) in json.loads(seen["a"][1])[0]["text"]
    assert str(b.workspace) in json.loads(seen["b"][1])[0]["text"]

