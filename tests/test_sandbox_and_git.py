"""run_python's guard (no processes, no file changes in the project) and the read-only git tool."""

import shutil
import subprocess

import pytest

from coding_agent import state
from coding_agent.common import ToolError
from coding_agent.tools import git as git_tool
from coding_agent.tools import python_runner


def run(code):
    return python_runner.tool_run_python(code=code)


@pytest.fixture
def allow_python(monkeypatch):
    monkeypatch.setattr(state, "always_allow_python", True)


@pytest.mark.parametrize("code, blocked", [
    ("import os; os.remove('keep.txt')", "os.remove"),
    ("import subprocess; subprocess.run(['echo', 'hi'])", "subprocess"),
    ("import os; os.system('echo hi')", "os.system"),
    ("open('keep.txt', 'w').write('x')", "open for writing"),
    ("import os; os.makedirs('newdir')", "os.mkdir"),
])
def test_guard_blocks_processes_and_file_changes(workspace, allow_python, code, blocked):
    (workspace / "keep.txt").write_text("keep")
    out = run(code)
    assert "Blocked by the coding agent" in out and blocked in out
    assert (workspace / "keep.txt").read_text() == "keep"
    assert not (workspace / "newdir").exists()


def test_guard_allows_reading_and_temp_files(workspace, allow_python):
    (workspace / "data.txt").write_text("42")
    out = run("import tempfile, pathlib\n"
              "t = pathlib.Path(tempfile.mkdtemp()) / 'x'; t.write_text('ok')\n"
              "print(open('data.txt').read(), t.read_text())")
    assert "exit code: 0" in out and "42 ok" in out


def test_run_python_asks_first(workspace, ui):
    ui.answers = ["no", "not now"]
    with pytest.raises(ToolError, match="not now"):
        run("print(1)")
    assert ui.of("confirm")[0][2] == ("yes", "no", "always for this session")


@pytest.fixture
def repo(workspace):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    g = ["git", "-C", str(workspace), "-c", "user.email=a@b", "-c", "user.name=T"]
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "app.py").write_text("print(1)\n")
    subprocess.run([*g, "add", "."], check=True)
    subprocess.run([*g, "commit", "-qm", "first"], check=True)
    (workspace / "app.py").write_text("print(2)\n")
    return workspace


def test_git_status_diff_log(repo):
    assert " M app.py" in git_tool.tool_git("status")
    assert "+print(2)" in git_tool.tool_git("diff")
    assert "first" in git_tool.tool_git("log")


def test_git_refuses_options_and_hostile_config(repo, tmp_path):
    with pytest.raises(ToolError, match="Invalid ref"):
        git_tool.tool_git("diff", ref="--output=/tmp/x")
    with pytest.raises(ToolError, match="read-only"):
        git_tool.tool_git("commit")
    marker = tmp_path / "PWNED"
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", f"touch {marker}"], check=True)
    git_tool.tool_git("status")
    assert not marker.exists()
