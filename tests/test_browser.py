"""screenshot_page: local pages are found and allowed, on every system (file:// URLs to paths)."""

import nturl2path
import urllib.request
from urllib.parse import urlsplit

from coding_agent.common import resolve
from coding_agent.tools import browser


def test_a_workspace_page_comes_back_to_its_own_path(workspace):
    """The browser asks for file:///C:/.../My%20Game/index.html; the request check must find the
    workspace file again (on Windows the '/' before the drive letter used to block every local page)."""
    page = workspace / "My Game - été" / "index.html"
    page.parent.mkdir()
    page.write_text("<h1>hi</h1>")
    url = page.as_uri()
    assert resolve(browser.file_url_path(urlsplit(url).path)) == page


def test_windows_file_urls(monkeypatch):
    monkeypatch.setattr(urllib.request, "url2pathname", nturl2path.url2pathname)
    path = "/C:/Users/demep/OneDrive%20-%20Hager%20Group/Desktop/game/index.html"
    assert browser.file_url_path(path) == r"C:\Users\demep\OneDrive - Hager Group\Desktop\game\index.html"


def test_a_page_that_reloads_itself_still_gives_a_screenshot(workspace, ui):
    """A game restarting with location.reload() used to crash the tool ("Execution context was
    destroyed"). Needs Playwright and a browser (AGENT_BROWSER_PATH); skipped without them."""
    import pytest

    pytest.importorskip("playwright")
    (workspace / "game.html").write_text("<canvas></canvas><script>setTimeout(() => location.reload(), 50)</script>")
    try:
        out = browser.tool_screenshot_page("game.html", wait_ms=300, full_page=True, include_text=True)
    except Exception as e:
        if "No browser could be started" in str(e) or "could not start" in str(e):
            pytest.skip("no browser here")
        raise
    assert [b["type"] for b in out].count("image") == 1
