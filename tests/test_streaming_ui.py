"""The terminal never loses the words of a streamed answer."""

import re

from coding_agent import ui


def shown(capsys):
    return re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)


def test_text_blocks_split_by_citations_lose_no_word_and_share_one_label(capsys):
    terminal = ui.TerminalUI()
    for block in ("financé à 90 % par des ressources ", "internes, avec une hausse de 31 % ", "dès 2026"):  # no trailing space in the last
        terminal.assistant_start()
        terminal.assistant_text(block.rstrip())  # a block ends in the middle of a sentence, on a word the printer is still holding
    terminal.assistant_end()
    out = shown(capsys)
    assert out.count("Claude:") == 1
    assert "ressources" in out and "31 %" in out and out.rstrip().endswith("2026")


def test_a_dropped_connection_still_shows_the_last_words(capsys):
    from coding_agent import loop, state
    from types import SimpleNamespace

    class Dropping:
        def __iter__(self):
            yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text"))
            yield SimpleNamespace(type="text", text="the answer stops in the mid")
            raise ConnectionError("dropped")

    state.ui = ui.TerminalUI()
    call = loop._Call()
    try:
        loop._show_events_guarded(Dropping(), call, state.ui)
    except ConnectionError:
        pass
    assert shown(capsys).strip().endswith("the answer stops in the mid")


def test_a_new_answer_after_a_tool_gets_its_own_label(capsys):
    terminal = ui.TerminalUI()
    terminal.assistant_start(); terminal.assistant_text("Searching"); terminal.tool_start("web_search")
    terminal.assistant_start(); terminal.assistant_text("Found it"); terminal.assistant_end()
    out = shown(capsys)
    assert out.count("Claude:") == 2 and "Searching" in out and "Found it" in out
