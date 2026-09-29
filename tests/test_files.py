"""File tools: approvals, diffs, refusals, self-protection, workspace confinement."""

import pytest

from coding_agent import state
from coding_agent.common import ToolError
from coding_agent.config import PACKAGE_DIR
from coding_agent.tools import files


def test_edit_shows_diff_and_applies_on_yes(workspace, ui):
    (workspace / "app.py").write_text("a = 1\nb = 2\n")
    ui.answers = ["yes"]
    result = files.tool_edit_file("app.py", "b = 2", "b = 3")
    assert "Modified app.py" in result
    assert (workspace / "app.py").read_text() == "a = 1\nb = 3\n"
    diff = ui.of("diff")[0]
    assert diff[1:4] == ("Modify", "app.py", 2)
    assert "-b = 2" in diff[4] and "+b = 3" in diff[4]
    assert ui.of("confirm")[0][1] == "Apply this change to app.py?"
    assert ("success", "Modified app.py") in ui.events


def test_edit_refused_passes_feedback_and_leaves_file(workspace, ui):
    (workspace / "app.py").write_text("x = 1\n")
    ui.answers = ["no", "use pathlib"]
    with pytest.raises(ToolError, match="use pathlib"):
        files.tool_edit_file("app.py", "x = 1", "x = 2")
    assert (workspace / "app.py").read_text() == "x = 1\n"
    assert ("failure", "app.py was not modified") in ui.events


def test_edit_detects_change_on_disk_while_waiting(workspace, ui):
    f = workspace / "app.py"
    f.write_text("a = 1\n")
    ui.answers = ["yes"]
    ui.on_confirm = lambda: f.write_text("a = 1\nmine = True\n")  # saved in the editor meanwhile
    with pytest.raises(ToolError, match="changed on disk"):
        files.tool_edit_file("app.py", "a = 1", "a = 2")
    assert f.read_text() == "a = 1\nmine = True\n"


def test_auto_mode_applies_without_asking(workspace, ui, monkeypatch):
    monkeypatch.setattr(state, "auto_mode", True)
    files.tool_write_file("new.py", "print(1)\n")
    assert (workspace / "new.py").exists()
    assert not ui.of("confirm")


def test_delete_always_asks_even_in_auto_mode(workspace, ui, monkeypatch):
    monkeypatch.setattr(state, "auto_mode", True)
    (workspace / "tmp.txt").write_text("x")
    ui.answers = ["no", ""]
    with pytest.raises(ToolError, match="NOT deleted"):
        files.tool_delete_file("tmp.txt")
    assert (workspace / "tmp.txt").exists()
    assert ui.of("confirm")[0][1] == "Delete tmp.txt?"


def test_paths_outside_workspace_are_refused(workspace):
    with pytest.raises(ToolError, match="outside the workspace"):
        files.tool_read_file("../secret.txt")
    with pytest.raises(ToolError, match="outside the workspace"):
        files.tool_write_file("../evil.py", "x")


def test_agent_never_modifies_its_own_package(workspace, monkeypatch):
    monkeypatch.setattr(state, "workspace", PACKAGE_DIR.parent)
    monkeypatch.setattr(state, "cwd", PACKAGE_DIR.parent)
    with pytest.raises(ToolError, match="own source code"):
        files.tool_write_file("coding_agent/loop.py", "boom")


def test_delete_folder_refusals(workspace, ui):
    (workspace / "nested" / ".git").mkdir(parents=True)
    with pytest.raises(ToolError, match="git repository"):
        files.tool_delete_folder("nested")
    with pytest.raises(ToolError, match="root cannot be deleted"):
        files.tool_delete_folder(".")


def test_delete_folder_asks_and_moves_cwd_back(workspace, ui, monkeypatch):
    (workspace / "build" / "sub").mkdir(parents=True)
    (workspace / "build" / "a.txt").write_text("a")
    monkeypatch.setattr(state, "cwd", workspace / "build" / "sub")
    ui.answers = ["yes"]
    result = files.tool_delete_folder("..")
    assert not (workspace / "build").exists()
    assert state.cwd == workspace and "now the repository root" in result
    assert ui.of("panel")[0][3] == "danger"


def test_copy_folder_skips_git_and_never_overwrites(workspace, ui):
    (workspace / "tpl" / ".git").mkdir(parents=True)
    (workspace / "tpl" / "a.txt").write_text("a")
    ui.answers = ["yes"]
    files.tool_copy_path("tpl", "copy")
    assert (workspace / "copy" / "a.txt").exists() and not (workspace / "copy" / ".git").exists()
    with pytest.raises(ToolError, match="onto or into itself"):
        files.tool_copy_path("tpl", "tpl/inner")


def test_grep_and_list(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "m.py").write_text("def hello():\n    return 1\n")
    assert "src/m.py:1: def hello():" in files.tool_grep("def hello")
    assert "src/" in files.tool_list_directory(".")
