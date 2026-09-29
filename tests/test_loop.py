"""The agent loop end to end, against a mocked Claude API (real SDK, fake HTTP)."""

import json

import httpx
import pytest
from anthropic import AnthropicFoundry

from coding_agent import config, context, memory, session, state
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
    assert first["system"] == f"You fill spreadsheets in {project.resolve()}."
    first_message = [b["text"] for b in first["messages"][0]["content"]]
    assert first_message[0].startswith("<memory>") and not any(b.startswith("<skills>") for b in first_message)
    results = {r["tool_use_id"]: r for r in fake.requests[1]["messages"][-1]["content"]}
    assert "hello" in results["toolu_0_read_file"]["content"]
    assert results["toolu_1_write_file"]["is_error"]  # not enabled in this session
    assert not (project / "x.txt").exists()
    assert ("text", "Done: notes.txt says hello.") in ui.events
    saved = json.loads(state.conversation_file.read_text())
    assert saved[-1]["content"][0]["text"].startswith("Done")


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
