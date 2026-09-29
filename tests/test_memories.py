"""Developers list, show and delete the agent's memory of each project."""

import os
import time

from coding_agent import memories


def project(home, name, notes="", conversation=False, age_days=0):
    folder = home / name
    (folder / "memories").mkdir(parents=True)
    if notes:
        (folder / "memories" / "notes.md").write_text(notes)
    if conversation:
        (folder / "conversation.json").write_text("[]")
    stamp = time.time() - age_days * 86_400
    for f in folder.rglob("*"):
        os.utime(f, (stamp, stamp))
    return folder


def test_list_shows_every_project_newest_first(tmp_path):
    project(tmp_path, "old-11111111", notes="# Old\n- uses ruff", age_days=10)
    project(tmp_path, "myapp-1a2b3c4d", notes="# My app\n- run with uv", conversation=True)
    text = memories.listing(tmp_path, current="myapp-1a2b3c4d")
    assert text.index("myapp-1a2b3c4d") < text.index("old-11111111")
    assert "* myapp-1a2b3c4d" in text and "conversation" in text and "-- My app" in text
    assert memories.listing(tmp_path / "none") .startswith("No memory yet")


def test_show_one_project_by_name_or_prefix(tmp_path):
    project(tmp_path, "myapp-1a2b3c4d", notes="# My app\n- run with uv")
    project(tmp_path, "mytool-99999999")
    assert "- run with uv" in memories.show(tmp_path, "myapp")
    assert "matches several memories" in memories.show(tmp_path, "my")
    assert "No memory named 'nope'" in memories.show(tmp_path, "nope")


def test_forget_asks_first_then_deletes(tmp_path):
    kept = project(tmp_path, "myapp-1a2b3c4d")
    other = project(tmp_path, "other-22222222")
    assert memories.forget(tmp_path, "myapp", confirm=lambda q: "n") == "Nothing deleted."
    assert kept.exists()
    assert memories.forget(tmp_path, "myapp", confirm=lambda q: "y") == "Deleted the memory of myapp-1a2b3c4d."
    assert not kept.exists() and other.exists()
    assert "Deleted the memory of other-22222222" in memories.forget(tmp_path, "all", confirm=lambda q: "yes")
    assert not other.exists()
