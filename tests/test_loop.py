"""The agent loop end to end, against a mocked Claude API (real SDK, fake HTTP)."""

import json

import httpx
import pytest
from anthropic import AnthropicFoundry

from coding_agent import config, context, loop, memory, session, state
from coding_agent.ui import HeadlessUI


def sse(blocks, stop, input_tokens=1000):
    """A streamed Messages API response: blocks are ("text", str) or (tool_name, input_dict)."""
    msg = {"id": "m", "type": "message", "role": "assistant", "model": "x", "content": [], "stop_reason": None,
           "stop_sequence": None, "usage": {"input_tokens": input_tokens, "output_tokens": 1,
                                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
    ev = [("message_start", {"type": "message_start", "message": msg})]
    for i, (kind, value) in enumerate(blocks):
        if kind == "text":
            ev += [("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": value}})]
        else:
            ev += [("content_block_start", {"type": "content_block_start", "index": i,
                                            "content_block": {"type": "tool_use", "id": f"toolu_{i}_{kind}", "name": kind, "input": {}}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": i,
                                            "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)}})]
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 5}}),
           ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in ev)


class FakeClaude:
    """Replays scripted responses and records every request the agent sends."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        if not body.get("stream"):  # memory curator / compaction summary
            return httpx.Response(200, json={"id": "c", "type": "message", "role": "assistant", "model": "x",
                                             "content": [{"type": "text", "text": "NO_CHANGE"}], "stop_reason": "end_turn",
                                             "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
        blocks, stop = self.script.pop(0)
        return httpx.Response(200, text=sse(blocks, stop), headers={"content-type": "text/event-stream"})


@pytest.fixture
def claude(monkeypatch):
    def install(script):
        fake = FakeClaude(script)
        client = AnthropicFoundry(api_key="k", base_url="https://x.services.ai.azure.com/anthropic",
                                  http_client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
        monkeypatch.setattr(session, "_get_client", lambda: client)
        monkeypatch.setattr(config, "MEMORY_UPDATES", False)
        monkeypatch.setattr(memory, "MEMORY_UPDATES", False)
        return fake
    return install


def test_session_with_tool_subset_and_custom_prompt(tmp_path, ui, claude, monkeypatch):
    project = tmp_path / "docs"
    project.mkdir()
    (project / "notes.txt").write_text("hello")
    monkeypatch.setattr(config, "MEMORY_HOME", tmp_path / "mem")
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    fake = claude([
        ([("read_file", {"path": "notes.txt"}), ("write_file", {"path": "x.txt", "content": "no"})], "tool_use"),
        ([("text", "Done: notes.txt says hello.")], "end_turn"),
    ])
    session.open_project(project, ui=ui, tools=["read_file", "ask_human"], system_prompt="You fill spreadsheets in {workspace}.")
    assert session.send("Read notes.txt")

    first = fake.requests[0]
    assert {t["name"] for t in first["tools"]} == {"read_file", "ask_human"}
    assert first["system"][0]["text"] == f"You fill spreadsheets in {project.resolve()}."  # with its own cache point
    first_message = [b["text"] for b in first["messages"][0]["content"]]
    assert first_message[0].startswith("<memory>") and not any(b.startswith("<skills>") for b in first_message)
    results = {r["tool_use_id"]: r for r in fake.requests[1]["messages"][-1]["content"]}
    assert "hello" in results["toolu_0_read_file"]["content"]
    assert results["toolu_1_write_file"]["is_error"]  # not enabled in this session
    assert not (project / "x.txt").exists()
    assert ("text", "Done: notes.txt says hello.") in ui.events
    saved = json.loads(state.conversation_file.read_text())
    assert saved[-1]["content"][0]["text"].startswith("Done")


def test_the_ui_is_told_why_each_response_stopped(tmp_path, ui, claude, monkeypatch):
    project = tmp_path / "docs"
    project.mkdir()
    (project / "notes.txt").write_text("hello")
    monkeypatch.setattr(config, "MEMORY_HOME", tmp_path / "mem")
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    claude([([("read_file", {"path": "notes.txt"})], "tool_use"), ([("text", "Done.")], "end_turn")])
    reasons = []
    ui.response_end = reasons.append
    session.open_project(project, ui=ui, tools=["read_file"])
    assert session.send("Read notes.txt")
    assert reasons == ["tool_use", "end_turn"]


def test_an_interrupted_stream_tells_the_ui_the_response_was_cut(ui):
    from types import SimpleNamespace

    reasons = []
    ui.response_end = reasons.append

    def stream():
        yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text"))
        yield SimpleNamespace(type="text", text="La moitié")
        raise ConnectionError("dropped")
    with pytest.raises(ConnectionError):
        loop._show_events_guarded(stream(), SimpleNamespace(cancelled=False, started=False), ui)
    assert reasons == ["interrupted"]


def test_failed_turn_is_rolled_back(tmp_path, claude, monkeypatch):
    project = tmp_path / "p"
    project.mkdir()
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    claude([])  # no scripted response: the request fails, as a network error would
    ui = HeadlessUI()
    messages_seen = []
    ui.message = messages_seen.append
    session.open_project(project, ui=ui)
    assert session.send("hi") is False
    assert session.messages == []  # the unfinished instruction is dropped, history stays valid
    assert any("network error" in m for m in messages_seen)


def test_context_clearing_replaces_old_tool_outputs(workspace, monkeypatch):
    monkeypatch.setattr(context, "KEEP_RECENT_RESULTS", 1)
    big = "x" * 1000
    messages = [{"role": "user", "content": "go"}]
    for i in range(3):
        messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "read_file", "input": {}}]})
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": big}]})
    assert context.clear_old_tool_results(messages) == 2
    assert messages[2]["content"][0]["content"] == config.CLEARED_NOTE
    assert messages[-1]["content"][0]["content"] == big


def test_stop_rolls_back_the_instruction(tmp_path, claude, monkeypatch):
    project = tmp_path / "p"
    project.mkdir()
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    fake = claude([([("list_directory", {"path": "."})], "tool_use"), ([("text", "never sent")], "end_turn")])
    ui = HeadlessUI()
    seen = []
    ui.message = seen.append
    ui.tool_start = lambda name: session.stop()  # the person presses Stop while the first reply streams
    session.open_project(project, ui=ui)
    assert session.send("list the files") is False
    assert "[interrupted]" in seen and session.messages == [] and len(fake.requests) == 1


def test_opening_another_project_starts_a_fresh_conversation(tmp_path, claude, monkeypatch):
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
    fake = claude([([("text", "one")], "end_turn"), ([("text", "two")], "end_turn")])
    session.open_project(tmp_path / "first", ui=HeadlessUI())
    session.send("hello")
    session.open_project(tmp_path / "second", ui=HeadlessUI())
    session.send("hello again")
    second = fake.requests[-1]["messages"]
    assert len(second) == 1  # not the first project's history
    assert second[0]["content"][0]["text"].startswith("<memory>")  # the new project's memory is sent


def test_every_tool_call_and_its_outcome_reach_the_ui(tmp_path, claude, monkeypatch):
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.txt").write_text("hello")
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    claude([([("read_file", {"path": "a.txt"}), ("read_file", {"path": "missing.txt"}),
              ("write_file", {"path": "b.txt", "content": "x" * 50})], "tool_use"),
            ([("text", "ok")], "end_turn")])
    calls = []
    ui = HeadlessUI()
    ui.tool_result = lambda *args: calls.append(args)
    session.open_project(project, ui=ui, tools=["read_file", "write_file"])
    session.send("go")
    assert calls[0] == ("read_file", "path='a.txt'", True, "11 characters")  # "    1\thello"
    assert calls[1][:3] == ("read_file", "path='missing.txt'", False) and "File not found" in calls[1][3]
    assert calls[2][1] == "path='b.txt', content=<50 chars>" and calls[2][2] is False  # refused by the headless UI


def test_stop_is_immediate_while_claude_has_not_answered_yet(tmp_path, claude, monkeypatch):
    # A large request being sent, or Claude thinking before its first word: no streamed chunk arrives
    # for a while. Stop must not wait for it.
    import threading
    import time
    project = tmp_path / "p"
    project.mkdir()
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    answer = FakeClaude.handler
    monkeypatch.setattr(FakeClaude, "handler", lambda self, request: (time.sleep(3), answer(self, request))[1])
    claude([([("text", "too late")], "end_turn")])
    ui = HeadlessUI()
    shown = []
    ui.assistant_text = shown.append
    session.open_project(project, ui=ui)
    threading.Timer(0.3, session.stop).start()
    started = time.monotonic()
    assert session.send("fill the workbook") is False
    assert time.monotonic() - started < 1.5  # not the 3 s Claude takes
    assert session.messages == []
    time.sleep(3.2)  # the abandoned call ends in the background...
    assert shown == []  # ...and shows nothing


def test_a_slow_first_answer_is_reported(tmp_path, claude, monkeypatch):
    # Claude (or the SDK's quiet retries of a timeout) takes long: the person sees it is still waiting.
    import time

    from coding_agent import loop
    project = tmp_path / "p"
    project.mkdir()
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    monkeypatch.setattr(loop, "WAIT_NOTICE_SECONDS", 1)
    answer = FakeClaude.handler
    monkeypatch.setattr(FakeClaude, "handler", lambda self, request: (time.sleep(2.3), answer(self, request))[1])
    claude([([("text", "hello")], "end_turn")])
    ui = HeadlessUI()
    notes = []
    ui.message = notes.append
    session.open_project(project, ui=ui)
    assert session.send("hello") is True
    assert [n for n in notes if "still waiting" in n] == ["[still waiting for Claude: 1 s]", "[still waiting for Claude: 2 s]"]


def test_the_request_carries_thinking_and_effort(tmp_path, ui, claude, monkeypatch):
    """Defaults: thinking off (disabled on claude-sonnet-5) and effort medium; the settings change the request, nothing else does."""
    monkeypatch.setattr(config, "MEMORY_HOME", tmp_path / "mem")
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    (tmp_path / "p").mkdir()

    def sent(**settings):
        for name in ("CODEAGENT_THINKING", "CODEAGENT_EFFORT"):
            monkeypatch.delenv(name, raising=False)
        for name, value in settings.items():
            monkeypatch.setenv(name, value)
        fake = claude([([("text", "ok")], "end_turn")])
        session.open_project(tmp_path / "p", ui=ui)
        session.send("hello")
        return next(r for r in fake.requests if r.get("stream"))

    body = sent()
    assert body["thinking"] == {"type": "disabled"} and body["output_config"] == {"effort": "medium"}
    body = sent(CODEAGENT_THINKING="adaptive", CODEAGENT_EFFORT="default")
    assert body["thinking"] == {"type": "adaptive"} and "output_config" not in body
    body = sent(CODEAGENT_EFFORT="low")
    assert body["output_config"] == {"effort": "low"}


def test_old_tool_outputs_are_cleared_only_when_that_frees_a_good_share_of_the_window(workspace, monkeypatch):
    """Each clearing loses the cached conversation from there on: it must be rare and large, not one output per call."""
    from coding_agent import state
    monkeypatch.setattr(context, "KEEP_RECENT_RESULTS", 1)
    monkeypatch.setattr(state, "context_window", 10_000)
    monkeypatch.setitem(state.context, "tokens", 6_000)          # above CLEAR_AT (50%)
    monkeypatch.setitem(state.context, "chars", 0)

    def history(size):
        messages = [{"role": "user", "content": "go"}]
        for i in range(3):
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "read_file", "input": {}}]})
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * size}]})
        return messages

    small = history(400)                                           # 2 old outputs, ~230 tokens: under 10% of the window
    monkeypatch.setitem(state.context, "chars", context.history_chars(small))
    context.manage_context(None, small)
    assert small[2]["content"][0]["content"] == "x" * 400          # left alone: not worth losing the cache
    big = history(4000)                                            # ~2,300 tokens to free: over 10%
    monkeypatch.setitem(state.context, "chars", context.history_chars(big))
    context.manage_context(None, big)
    assert big[2]["content"][0]["content"] == config.CLEARED_NOTE and big[4]["content"][0]["content"] == config.CLEARED_NOTE
    assert big[-1]["content"][0]["content"] == "x" * 4000          # the latest output stays


def test_the_system_prompt_has_its_own_cache_point(workspace, monkeypatch):
    from coding_agent import loop, state
    monkeypatch.setattr(state, "system_prompt", "You help. Workspace: {workspace}")
    for name in ("DEEPSEEK_API_KEY", "AGENT_CACHE_TTL", "ANTHROPIC_BASE_URL", "CODEAGENT_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    block = loop.system_param()
    assert block == [{"type": "text", "text": f"You help. Workspace: {state.workspace}", "cache_control": {"type": "ephemeral"}}]
    monkeypatch.setenv("AGENT_CACHE_TTL", "1h")
    assert loop.system_param()[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_ENDPOINT", raising=False)
    assert loop.system_param() == f"You help. Workspace: {state.workspace}"   # DeepSeek: no cache_control


def test_page_images_read_in_a_turn_become_a_reference_in_the_history_but_text_reads_stay(tmp_path, ui, claude, monkeypatch):
    from coding_agent.tools import documents
    from test_documents import make_pdf
    project = tmp_path / "docs"
    project.mkdir()
    make_pdf(project / "a.pdf", ["Total 642.00 EUR on page one", "Second page text"])
    make_pdf(project / "scan.pdf", ["", ""])  # no text layer: read as pages
    monkeypatch.setattr(documents, "uses_deepseek", lambda: False)
    monkeypatch.setattr(config, "MEMORY_HOME", tmp_path / "mem")
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    fake = claude([([("read_pdf", {"path": "a.pdf", "pages": "1", "mode": "text"}), ("read_pdf", {"path": "scan.pdf", "pages": "1", "mode": "visual"})],
                    "tool_use"), ([("text", "Done.")], "end_turn"), ([("text", "Same.")], "end_turn")])
    session.open_project(project, ui=ui, tools=["read_pdf"])
    assert session.send("Read both.")
    seen = json.dumps(fake.requests[1]["messages"])           # inside the turn the model saw both
    assert "Total 642.00 EUR on page one" in seen and '"type": "document"' in seen
    kept = json.dumps(session.messages)                        # after the turn: the text stays, the pages are a reference
    assert "Total 642.00 EUR on page one" in kept
    assert '"type": "document"' not in kept and "scan.pdf, pages 1, read as images" in kept
    assert session.send("Repeat.")
    assert '"type": "document"' not in json.dumps(fake.requests[2]["messages"])
