"""What the agent keeps on the machine is cleaned, so nothing grows forever."""

import os
import time

from coding_agent import cleanup

DAY = 86_400
NOW = time.time()


def file(path, age_days, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
    return path


def test_backups_older_than_three_days_go(tmp_path):
    backups = tmp_path / "backups"
    project = backups / "myapp-1a2b3c4d"
    old = file(project / "20260920-100000-costs.xlsx", 4)
    only_copy = file(project / "20250101-100000-grid.xlsx", 300)  # even the only copy of a workbook
    recent = file(project / "20260928-100000-costs.xlsx", 1)
    stale = file(backups / "old-99999999" / "20250101-100000-a.xlsx", 10)
    assert cleanup.clean_backups(backups, cleanup.BACKUP_DAYS, NOW) == 3
    assert not old.exists() and not only_copy.exists() and not stale.exists()
    assert recent.exists()
    assert not stale.parent.exists()  # empty folders go too


def test_unused_project_memories_and_old_conversations_go(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    unused = memory / "old-11111111"
    file(unused / "memories" / "notes.md", 120)
    file(unused / "conversation.json", 100)
    notes = file(memory / "recent-22222222" / "memories" / "notes.md", 200)  # old notes...
    old_conversation = file(memory / "recent-22222222" / "conversation.json", 40)
    file(memory / "recent-22222222" / "memories" / "other.md", 5)  # ...but the project was used 5 days ago
    summary = cleanup.run(backups=tmp_path / "backups", memory=memory, now=NOW)
    assert not unused.exists()
    assert notes.exists() and not old_conversation.exists()
    assert summary == "Clean-up: 1 unused project memories, 1 old saved conversation(s)"

    monkeypatch.setenv("AGENT_MEMORY_DAYS", "1")  # configurable
    cleanup.run(backups=tmp_path / "backups", memory=memory, now=NOW)
    assert not notes.exists()


def test_durations_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_BACKUP_DAYS", "7")
    assert cleanup.days("AGENT_BACKUP_DAYS", 30) == 7
    monkeypatch.setenv("AGENT_BACKUP_DAYS", "not a number")
    assert cleanup.days("AGENT_BACKUP_DAYS", 30) == 30
    monkeypatch.setenv("AGENT_BACKUP_DAYS", "0")
    assert cleanup.days("AGENT_BACKUP_DAYS", 30) == 1


def test_nothing_to_clean(tmp_path):
    assert cleanup.run(backups=tmp_path / "b", memory=tmp_path / "m", now=NOW) == "Clean-up: nothing to delete"


def test_given_its_folders_it_does_not_load_the_settings(tmp_path):
    import subprocess
    import sys
    code = ("import sys, pathlib; from coding_agent import cleanup; "
            f"cleanup.run(backups=pathlib.Path({str(tmp_path / 'b')!r}), memory=pathlib.Path({str(tmp_path / 'm')!r})); "
            "print('coding_agent.config' in sys.modules)")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip() == "False"
