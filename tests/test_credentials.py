"""Logins are saved owner-only in ~/.coding-agent/secrets and reused, so the person is asked once."""

import json
import stat

from coding_agent import credentials, state
from coding_agent.tools import mail, sms
from tests.conftest import ScriptedUI
from tests.test_mail import FakeSMTP  # noqa: F401  (the fixtures below patch smtplib with it)
from tests.test_mail import smtp  # noqa: F401


def test_files_are_private_and_can_be_forgotten(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SECRETS_DIR", str(tmp_path / "secrets"))
    path = credentials.save("mail", {"user": "a@b.com", "password": "pw"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert credentials.load("mail") == {"user": "a@b.com", "password": "pw"} and credentials.load("nothing") == {}
    assert credentials.forget() == ["mail"] and credentials.load("mail") == {}


def test_mail_login_saves_then_reuses_the_login_without_asking(tmp_path):
    state.ui = ScriptedUI(["me@gmail.com", "pw"])
    mail.tool_mail_login()
    assert json.loads((tmp_path / "secrets" / "mail.json").read_text()) == {"user": "me@gmail.com", "password": "pw"}
    mail._session.clear()
    state.ui = ScriptedUI([])  # any question would fail the test
    assert "me@gmail.com" in mail.tool_mail_login()


def test_new_login_asks_again_and_a_refused_saved_login_is_deleted(tmp_path):
    credentials.save("mail", {"user": "old@gmail.com", "password": "old"})
    state.ui = ScriptedUI(["new@gmail.com", "pw"])
    assert "new@gmail.com" in mail.tool_mail_login(new_login=True)
    FakeSMTP.refuse = True
    state.ui = ScriptedUI([])
    try:
        mail.tool_mail_login()
    except Exception:
        pass
    assert credentials.load("mail") == {}


def test_sms_login_is_saved_and_reused(monkeypatch):
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
        monkeypatch.delenv(k, raising=False)
    sms._session.clear()
    monkeypatch.setattr(sms.urllib.request, "urlopen", lambda r, timeout=None: __import__("io").BytesIO(b'{"sid": "SM1", "status": "queued"}'))
    state.ui = ScriptedUI(["AC1", "tok", "+15551234567", "yes"])
    sms.tool_send_sms("+33612345678", "Hi")
    sms._session.clear()
    state.ui = ScriptedUI(["yes"])  # only the approval: the login comes from the file
    sms.tool_send_sms("+33612345678", "Hi again")
    assert credentials.load("twilio")["TWILIO_AUTH_TOKEN"] == "tok"
