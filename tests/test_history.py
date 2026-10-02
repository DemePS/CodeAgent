"""Up arrow at the "You:" prompt: what is remembered, and what is not."""

import pytest

from coding_agent import history


class FakeReadline:
    def __init__(self):
        self.items = []
        self.saved = None

    def get_current_history_length(self):
        return len(self.items)

    def get_history_item(self, i):
        return self.items[i - 1]

    def add_history(self, line):
        self.items.append(line)

    def write_history_file(self, path):
        self.saved = path


@pytest.fixture
def fake(monkeypatch):
    fake = FakeReadline()
    monkeypatch.setattr(history, "_readline", fake)
    return fake


def test_remembers_the_first_line_once_in_a_row(fake):
    for text in ("fill costs.xlsx\nsecond line", "fill costs.xlsx", "", "   ", '"""', "next"):
        history.remember(text)
    assert fake.items == ["fill costs.xlsx", "next"]


def test_nothing_without_readline(monkeypatch):
    monkeypatch.setattr(history, "_readline", None)
    history.remember("x")
    history.save()
    assert history.prompt("\033[1mYou:\033[0m ") == "\033[1mYou:\033[0m "


def test_colour_codes_are_marked_for_readline(fake):
    assert history.prompt("\033[1mYou:\033[0m ") == "\001\033[1m\002You:\001\033[0m\002 "


def test_saved_to_the_agent_folder(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "sub" / "history")
    history.save()
    assert fake.saved == tmp_path / "sub" / "history"
