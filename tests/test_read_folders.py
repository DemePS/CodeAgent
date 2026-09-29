"""Read-only folders: the agent may read them (by absolute path) but never write there."""

import pytest

from coding_agent import session, state
from coding_agent.common import ToolError
from coding_agent.tools import documents, files


@pytest.fixture
def shared(tmp_path, workspace):
    folder = tmp_path / "shared-docs"
    folder.mkdir()
    (folder / "notes.txt").write_text("total 642.00 EUR\n")
    (folder / "sub").mkdir()
    (folder / "sub" / "more.txt").write_text("more\n")
    return folder.resolve()


def test_reading_a_read_only_folder(workspace, shared):
    with pytest.raises(ToolError, match="outside the workspace"):
        files.tool_read_file(str(shared / "notes.txt"))
    session.add_read_folder(shared)
    assert "642.00" in files.tool_read_file(str(shared / "notes.txt"))
    assert "more.txt" in files.tool_list_directory(str(shared / "sub"))
    assert f"{shared.as_posix()}/notes.txt:1:" in files.tool_grep("642", path=str(shared))


def test_never_writing_there(workspace, shared, ui):
    session.add_read_folder(shared)
    ui.answers = ["yes", "yes", "yes"]
    with pytest.raises(ToolError, match="outside the workspace"):
        files.tool_write_file(str(shared / "evil.txt"), "x")
    with pytest.raises(ToolError, match="outside the workspace"):
        files.tool_edit_file(str(shared / "notes.txt"), "642.00", "0.00")
    with pytest.raises(ToolError, match="outside the workspace"):
        files.tool_delete_file(str(shared / "notes.txt"))
    with pytest.raises(ToolError, match="outside the workspace"):
        documents.tool_edit_excel(str(shared / "book.xlsx"), [{"cell": "A1", "value": 1}])
    assert (shared / "notes.txt").read_text() == "total 642.00 EUR\n"
    assert not (shared / "evil.txt").exists()


def test_other_folders_stay_closed(workspace, shared, tmp_path):
    session.add_read_folder(shared)
    (tmp_path / "private.txt").write_text("secret")
    with pytest.raises(ToolError, match="outside the workspace and the read-only folders"):
        files.tool_read_file(str(tmp_path / "private.txt"))
    with pytest.raises(ToolError):
        files.tool_read_file(str(shared / ".." / "private.txt"))


def test_copying_from_a_read_only_folder_into_the_workspace(workspace, shared, ui):
    session.add_read_folder(shared)
    ui.answers = ["yes"]
    files.tool_copy_path(str(shared / "notes.txt"), "notes-copy.txt")
    assert (workspace / "notes-copy.txt").read_text() == "total 642.00 EUR\n"


def test_claude_is_told_and_a_new_project_starts_without_them(workspace, shared, tmp_path):
    session.add_read_folder(shared)
    session.add_read_folder(shared / "sub")  # already covered: not listed twice
    assert state.read_roots == [shared]
    assert state.read_roots_note.startswith("<read_only_folders>") and shared.as_posix() in state.read_roots_note
    other = tmp_path / "other-project"
    other.mkdir()
    state.set_workspace(other.resolve(), "other", tmp_path / "mem")
    assert state.read_roots == [] and state.read_roots_note is None
