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


# --- the terminal agent asks before reading elsewhere

def questions(ui):
    return [e[1] for e in ui.events if e[0] == "confirm"]


@pytest.fixture
def asking(monkeypatch):
    monkeypatch.setattr(state, "ask_read_outside", True)


def test_asks_once_per_folder_and_remembers(workspace, shared, ui, asking):
    ui.answers = ["yes"]
    assert "642.00" in files.tool_read_file(str(shared / "notes.txt"))
    assert "more.txt" in files.tool_list_directory(str(shared / "sub"))  # no second question
    assert state.read_roots == [shared] and "read" in questions(ui)[0].lower()
    assert len(questions(ui)) == 1


def test_refusal_is_remembered_and_nothing_is_read(workspace, shared, ui, asking):
    ui.answers = ["no"]
    with pytest.raises(ToolError, match="did not allow"):
        files.tool_read_file(str(shared / "notes.txt"))
    with pytest.raises(ToolError, match="did not allow"):
        files.tool_read_file(str(shared / "notes.txt"))
    assert len(questions(ui)) == 1 and state.read_roots == []


def test_writing_outside_is_still_refused(workspace, shared, ui, asking):
    ui.answers = ["yes", "yes", "yes"]
    files.tool_read_file(str(shared / "notes.txt"))
    with pytest.raises(ToolError, match="outside the workspace"):
        files.tool_write_file(str(shared / "evil.txt"), "x")


def test_credentials_are_never_read_even_when_allowed(workspace, tmp_path, monkeypatch, ui, asking):
    from coding_agent import common
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE")
    (home / "project.env").write_text("x")
    (home / ".env").write_text("KEY=1")
    monkeypatch.setattr(common, "HOME_DIR", home.resolve())
    ui.answers = ["yes"] * 4
    for secret in (home / ".ssh" / "id_rsa", home / ".env"):
        with pytest.raises(ToolError, match="never read"):
            files.tool_read_file(str(secret))
    assert questions(ui) == []
