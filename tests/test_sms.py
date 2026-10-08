"""send_sms asks first, validates the number, and posts to Twilio."""

import io
import json

import pytest

from coding_agent import state
from coding_agent.common import ToolError
from coding_agent.tools import sms
from tests.conftest import ScriptedUI


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def twilio(monkeypatch):
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
        monkeypatch.delenv(k, raising=False)
    sms._session.clear()
    sent = []

    def urlopen(request, timeout=None):
        sent.append(request)
        return FakeResponse(json.dumps({"sid": "SM1", "status": "queued"}).encode())

    monkeypatch.setattr(sms.urllib.request, "urlopen", urlopen)
    return sent


def test_sends_after_approval(twilio):
    state.ui = ScriptedUI(["AC123", "tok", "+15551234567", "yes"])
    out = sms.tool_send_sms("+33 6 12 34 56 78", "Bonjour")
    assert b"To=%2B33612345678" in twilio[0].data and "SM1" in out


def test_refusal_sends_nothing(twilio):
    state.ui = ScriptedUI(["AC123", "tok", "+15551234567", "no", ""])
    with pytest.raises(ToolError):
        sms.tool_send_sms("+33612345678", "Hi")
    assert not twilio


def test_credentials_are_asked_only_once_per_run(twilio):
    state.ui = ScriptedUI(["AC123", "tok", "+15551234567", "yes", "yes"])
    sms.tool_send_sms("+33612345678", "One")
    sms.tool_send_sms("+33612345678", "Two")
    assert len(twilio) == 2


def test_environment_skips_the_questions(twilio, monkeypatch):
    for k, v in {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "TWILIO_FROM": "+15551234567"}.items():
        monkeypatch.setenv(k, v)
    state.ui = ScriptedUI(["yes"])
    sms.tool_send_sms("+33612345678", "Hi")
    assert len(twilio) == 1


def test_bad_recipient_and_empty_answer():
    state.ui = ScriptedUI(["AC123", "tok", "+15551234567", "yes"])
    with pytest.raises(ToolError):
        sms.tool_send_sms("0612345678", "Hi")
    sms._session.clear()
    state.ui = ScriptedUI([""])
    with pytest.raises(ToolError):
        sms.tool_send_sms("+33612345678", "Hi")
