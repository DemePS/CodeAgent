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
