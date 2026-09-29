"""Network safety rules, and the terminal UI's prompts."""

import builtins
import socket

import pytest

from coding_agent import state
from coding_agent.common import ToolError
from coding_agent.tools import network
from coding_agent.ui import TerminalUI


def test_download_refuses_metadata_and_non_http(workspace):
    with pytest.raises(ToolError, match="metadata"):
        network.tool_download_file("http://169.254.169.254/metadata/identity/oauth2/token")
    with pytest.raises(ToolError, match="Only http"):
        network.tool_download_file("file:///etc/passwd")


def test_download_always_asks_even_in_auto_mode(workspace, ui, monkeypatch):
    monkeypatch.setattr(state, "auto_mode", True)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))])
    ui.answers = ["no", "no network"]
    with pytest.raises(ToolError, match="no network"):
        network.tool_download_file("https://example.com/data.csv")
    assert ui.of("panel")[0][3] == "network"


@pytest.mark.parametrize("url", ["ext::sh -c touch% /tmp/x", "file:///etc", "/home/user/repo",
                                 "--upload-pack=touch /tmp/x", "git@-oProxyCommand=x:repo"])
def test_clone_refuses_dangerous_urls(workspace, url):
    with pytest.raises(ToolError):
        network.tool_clone_repo(url)


def test_terminal_confirm_maps_letters_and_defaults_to_no(monkeypatch, capsys):
    ui = TerminalUI()
    answers = iter(["y", "a", "", "maybe"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": (print(prompt, end=""), next(answers))[1])
    assert ui.confirm("Delete x?") == "yes"
    assert ui.confirm("Run this?", ("yes", "no", "always for this session")) == "always for this session"
    assert ui.confirm("Apply?") == "no"
    assert ui.confirm("Apply?") == "no"
    out = capsys.readouterr().out
    assert "Delete x? [y]es / [n]o: " in out
    assert "Run this? [y]es / [n]o / [a]lways for this session: " in out


def test_terminal_renders_diff_and_cells(capsys, tmp_path):
    ui = TerminalUI()
    ui.diff("Modify", "app.py", tmp_path / "app.py", 2, ["--- a/app.py", "+++ b/app.py", "@@ -1 +1 @@", "-a", "+b"])
    ui.cell_changes("Modify workbook costs.xlsx (1 cell(s))", [("Costs!B2", "", "12", "0.00")], 0)
    out = capsys.readouterr().out
    assert "=== Modify \x1b[0mapp.py:2" in out and "\x1b[32m+b\x1b[0m" in out
    assert "Costs!B2" in out and "(empty)" in out and "[0.00]" in out
