"""When Claude cannot be reached: plain explanations, a connection check, nothing crashes."""

import httpx
import pytest
from anthropic import AnthropicFoundry

from coding_agent import errors, session
from coding_agent.ui import HeadlessUI

ENDPOINT = "https://x.services.ai.azure.com/anthropic"


def client_answering(status: int, body: dict):
    return AnthropicFoundry(api_key="k", base_url=ENDPOINT, max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))))


def unreachable_client():
    def fail(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in chain")
    return AnthropicFoundry(api_key="k", base_url=ENDPOINT, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(fail)))


@pytest.fixture
def project(tmp_path, monkeypatch):
    # A developer machine may have a project .env selecting DeepSeek: this suite is about Claude/Foundry.
    monkeypatch.delenv("CODEAGENT_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(session, "MEMORY_HOME", tmp_path / "mem")
    folder = tmp_path / "p"
    folder.mkdir()
    return folder


@pytest.mark.parametrize("status, body, expected", [
    (401, {"error": {"code": "401", "message": "invalid subscription key"}}, "Access denied by https://x.services.ai.azure.com/anthropic (HTTP 401)"),
    (404, {"type": "error", "error": {"type": "not_found_error", "message": "Deployment not found"}}, "no deployment named"),
    (408, {"error": {"code": "Timeout", "message": "The operation was timeout."}}, "did not answer in time"),
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, "rate limit"),
    (503, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, "retry in a moment"),
])
def test_api_errors_are_explained(project, monkeypatch, status, body, expected):
    monkeypatch.setattr(session, "_get_client", lambda: client_answering(status, body))
    errors_seen = []
    ui = HeadlessUI()
    ui.error = errors_seen.append
    session.open_project(project, ui=ui)
    assert session.send("hello") is False
    assert len(errors_seen) == 1 and expected in errors_seen[0]
    assert session.messages == []


def test_network_errors_name_the_endpoint_and_the_cause(project, monkeypatch):
    monkeypatch.setattr(session, "_get_client", unreachable_client)
    ok, why = session.check_connection()
    assert not ok
    assert ENDPOINT in why and "CERTIFICATE_VERIFY_FAILED" in why and "company proxy" in why


def test_check_connection_ok(project, monkeypatch):
    reply = {"id": "m", "type": "message", "role": "assistant", "model": "x", "content": [{"type": "text", "text": "p"}],
             "stop_reason": "max_tokens", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
    monkeypatch.setattr(session, "_get_client", lambda: client_answering(200, reply))
    ok, summary = session.check_connection()
    assert ok and ENDPOINT in summary and "deployment" in summary


def test_failed_microsoft_sign_in_is_explained(monkeypatch):
    from azure.core.exceptions import ClientAuthenticationError
    error = ClientAuthenticationError("DefaultAzureCredential failed to retrieve a token")
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN", raising=False)
    assert "sign in with your work account" in errors.describe(error)
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN", "0")  # no sign-in page (servers): developer hints
    assert "az login" in errors.describe(error)


def test_unrelated_errors_are_not_hidden(project, monkeypatch):
    def broken():
        raise ValueError("a bug")
    monkeypatch.setattr(session, "_get_client", broken)
    session.open_project(project, ui=HeadlessUI())
    with pytest.raises(ValueError):
        session.send("hello")


def test_check_names_the_first_step_that_fails(project, monkeypatch):
    # Foundry answers a plain request but times out once thinking is asked for.
    from coding_agent import diagnose

    reply = {"id": "m", "type": "message", "role": "assistant", "model": "x", "content": [{"type": "text", "text": "hi"}],
             "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
    stream = ("event: message_start\ndata: " + __import__("json").dumps({"type": "message_start", "message": {**reply, "content": [], "stop_reason": None}})
              + "\n\nevent: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")

    def answer(request):
        body = __import__("json").loads(request.content)
        if "thinking" in body:
            return httpx.Response(408, json={"error": {"code": "Timeout", "message": "The operation was timeout."}})
        if body.get("stream"):
            return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=reply)

    client = AnthropicFoundry(api_key="k", base_url=ENDPOINT, http_client=httpx.Client(transport=httpx.MockTransport(answer)))
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setattr(diagnose, "_get_client", lambda: client)
    session.open_project(project, ui=HeadlessUI())
    lines = []
    assert diagnose.run_check(print=lambda *a, **k: lines.append(" ".join(map(str, a)))) is False
    text = "\n".join(lines)
    assert text.count("ok (") == 3  # sign-in, plain request, streaming
    assert "FAILED" in text and "did not answer in time" in text
    assert "agent's request" not in text  # stopped at the first failure
