"""send_mail asks first, validates addresses, and sends over SMTP."""

import pytest

from coding_agent import state
from coding_agent.common import ToolError
from coding_agent.tools import mail
from tests.conftest import ScriptedUI


class FakeSMTP:
    sent = []
    hosts = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        FakeSMTP.hosts.append(host)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message, to_addrs):
        FakeSMTP.sent.append((message, to_addrs))


@pytest.fixture(autouse=True)
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.hosts = [], []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "SMTP_PORT"):
        monkeypatch.delenv(k, raising=False)
    mail._session.clear()


def test_sends_after_approval():
    state.ui = ScriptedUI(["me@gmail.com", "pw", "yes"])
    out = mail.tool_send_mail("a@b.com, c@d.fr", "Visite", "Bonjour", bcc="e@f.com")
    message, to_addrs = FakeSMTP.sent[0]
    assert to_addrs == ["a@b.com", "c@d.fr", "e@f.com"] and message["Subject"] == "Visite" and "Bcc" not in message
    assert "a@b.com" in out and FakeSMTP.hosts == ["smtp.gmail.com"]


def test_unknown_domain_builds_smtp_host_from_the_address():
    assert mail.smtp_host_for("Awa@Exemple.sn") == "smtp.exemple.sn"
    assert mail.smtp_host_for("x@hotmail.fr") == "smtp-mail.outlook.com"


def test_address_and_password_are_asked_only_once_per_run():
    state.ui = ScriptedUI(["me@gmail.com", "pw", "yes", "yes"])
    mail.tool_send_mail("a@b.com", "One", "x")
    mail.tool_send_mail("a@b.com", "Two", "x")
    assert len(FakeSMTP.sent) == 2


def test_refusal_sends_nothing():
    state.ui = ScriptedUI(["me@gmail.com", "pw", "no", ""])
    with pytest.raises(ToolError):
        mail.tool_send_mail("a@b.com", "Hi", "x")
    assert not FakeSMTP.sent


def test_bad_recipient_and_bad_own_address():
    state.ui = ScriptedUI(["nope"])
    with pytest.raises(ToolError):
        mail.tool_send_mail("not-an-address", "Hi", "x")
    with pytest.raises(ToolError):
        mail.tool_send_mail("a@b.com", "Hi", "x")


def test_falls_back_to_port_465_when_587_drops_the_connection(monkeypatch):
    ports = []

    def deliver(host, port, *args):
        ports.append(port)
        if port == 587:
            raise mail.smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr(mail, "_deliver", deliver)
    state.ui = ScriptedUI(["me@gmail.com", "pw", "yes"])
    mail.tool_send_mail("a@b.com", "Hi", "x")
    assert ports == [587, 465]


def test_a_refused_login_forgets_the_password_and_says_why(monkeypatch):
    def deliver(*args):
        raise mail.smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(mail, "_deliver", deliver)
    state.ui = ScriptedUI(["me@gmail.com", "wrong", "yes"])
    with pytest.raises(ToolError, match="app password"):
        mail.tool_send_mail("a@b.com", "Hi", "x")
    assert "password" not in mail._session


def test_every_step_is_logged_in_the_ui_without_the_password():
    state.ui = ScriptedUI(["me@gmail.com", "s3cret-pw", "yes"])
    mail.tool_send_mail("a@b.com", "Hi", "x")
    logs = [e[1] for e in state.ui.events if e[0] == "status" and e[1].startswith("[mail]")]
    assert any("connecting to smtp.gmail.com:587" in l for l in logs) and any("logged in as me@gmail.com" in l for l in logs)
    assert not any("s3cret-pw" in str(e) for e in state.ui.events)
