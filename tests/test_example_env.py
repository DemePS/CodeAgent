"""example.env lists every environment variable the package reads."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPERATING_SYSTEM = {"HOME", "DISPLAY", "WAYLAND_DISPLAY", "PATH"}  # not settings of the agent


def read_in_code() -> set[str]:
    """The names of the variables the code reads: os.environ[...], os.environ.get(...), os.getenv(...), and cleanup.days(...)."""
    names = set()
    for path in [*(ROOT / "coding_agent").rglob("*.py"), *(ROOT / "scripts").glob("*.py"), ROOT / "agent.py"]:
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                if ast.unparse(node.value).endswith("environ"):
                    names.add(node.slice.value)
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                called = ast.unparse(node.func)
                if called.endswith(("environ.get", "getenv")) or called in ("days", "cleanup.days"):
                    names.add(node.args[0].value)
    return {n for n in names if re.fullmatch(r"[A-Z][A-Z0-9_]+", n)} - OPERATING_SYSTEM


def listed_in_example_env() -> set[str]:
    """NAME= lines of example.env, with or without a leading '# '."""
    text = (ROOT / "example.env").read_text(encoding="utf-8")
    return set(re.findall(r"^(?:#\s*)?([A-Z][A-Z0-9_]+)\b", text, re.MULTILINE)) | set(re.findall(r"^(?:#\s*)?([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))


def test_every_variable_the_code_reads_is_in_example_env():
    missing = read_in_code() - listed_in_example_env()
    assert not missing, f"add these to example.env: {sorted(missing)}"


def test_the_finder_sees_the_known_variables():
    found = read_in_code()
    assert {"ANTHROPIC_API_KEY", "CODEAGENT_THINKING", "CODEAGENT_EFFORT", "AGENT_BACKUP_DAYS"} <= found


def test_the_defaults_in_example_env_are_the_defaults_of_the_code():
    from coding_agent import config
    text = (ROOT / "example.env").read_text(encoding="utf-8")
    assert f"CODEAGENT_THINKING={config.DEFAULT_THINKING}" in text and f"CODEAGENT_EFFORT={config.DEFAULT_EFFORT}" in text
    assert f"Default: {config.DEFAULT_MODEL}," in text
