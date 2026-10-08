"""mail_login asks the person and checks the login; send_mail needs it, asks for approval, and sends over SMTP."""

import pytest

from coding_agent import state
from coding_agent.common import ToolError
from coding_agent.tools import mail
from tests.conftest import ScriptedUI


class FakeSMTP:
    sent, hosts, logins, auth_offered, refuse = [], [], [], None, False
    esmtp_features = {"auth": "LOGIN PLAIN XOAUTH2"}

    def __init__(self, host, port, timeout=None):
        FakeSMTP.hosts.append((host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def quit(self):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        FakeSMTP.auth_offered = self.esmtp_features["auth"]
        if FakeSMTP.refuse:
            raise mail.smtplib.SMTPAuthenticationError(535, b"bad credentials")
        FakeSMTP.logins.append((user, password))

    def send_message(self, message, to_addrs):
        FakeSMTP.sent.append((message, to_addrs))


@pytest.fixture(autouse=True)
def smtp(monkeypatch, tmp_path):
    FakeSMTP.sent, FakeSMTP.hosts, FakeSMTP.logins, FakeSMTP.refuse = [], [], [], False
    monkeypatch.setenv("AGENT_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "SMTP_PORT"):
        monkeypatch.delenv(k, raising=False)
    mail._session.clear()


def login(*extra):
    state.ui = ScriptedUI(["me@gmail.com", "pw", *extra])
    return mail.tool_mail_login()


def test_login_asks_the_person_derives_the_host_and_checks_the_credentials():
    assert "me@gmail.com" in login()
    assert FakeSMTP.hosts == [("smtp.gmail.com", 587)] and FakeSMTP.logins == [("me@gmail.com", "pw")] and not FakeSMTP.sent
    assert FakeSMTP.auth_offered == "PLAIN"  # a refused PLAIN is not hidden behind a dropped AUTH LOGIN


def test_without_a_login_send_mail_opens_a_gmail_draft_in_the_browser_and_says_it_was_not_sent(monkeypatch):
    opened = []
    monkeypatch.setattr(mail, "_open_url", lambda url: opened.append(url) or True)
    monkeypatch.setattr(mail, "_last_address", "me@gmail.com")
    state.ui = ScriptedUI(["yes"])
    out = mail.tool_send_mail("a@b.com", "Visite & plus", "Bonjour\nà bientôt", cc="c@d.fr")
    url = opened[0]
    assert url.startswith("https://mail.google.com/mail/?view=cm&fs=1&") and "to=a%40b.com" in url and "cc=c%40d.fr" in url
    assert "su=Visite%20%26%20plus" in url and "body=Bonjour%0A" in url
    assert "NOT sent" in out and not FakeSMTP.sent and not FakeSMTP.hosts


def test_the_draft_uses_mailto_for_other_providers_and_needs_approval(monkeypatch):
    opened = []
    monkeypatch.setattr(mail, "_open_url", lambda url: opened.append(url) or True)
    monkeypatch.setattr(mail, "_last_address", "me@orange.fr")
    state.ui = ScriptedUI(["no", ""])
    with pytest.raises(ToolError):
        mail.tool_send_mail("a@b.com", "Hi", "x")
    assert not opened
    state.ui = ScriptedUI(["yes"])
    mail.tool_send_mail("a@b.com", "Hi", "x")
    assert opened[0].startswith("mailto:a%40b.com?subject=Hi&body=x")


def test_a_message_too_long_for_a_link_is_refused(monkeypatch):
    monkeypatch.setattr(mail, "_open_url", lambda url: True)
    state.ui = ScriptedUI(["yes"])
    with pytest.raises(ToolError, match="too long"):
        mail.tool_send_mail("a@b.com", "Hi", "x" * 8000)


def test_a_refused_login_points_to_the_draft_fallback():
    FakeSMTP.refuse = True
    state.ui = ScriptedUI(["me@gmail.com", "wrong"])
    with pytest.raises(ToolError, match="draft"):
        mail.tool_mail_login()
    assert mail._last_address == "me@gmail.com"


def test_sends_after_login_and_approval_without_asking_the_credentials_again():
    login()
    state.ui = ScriptedUI(["yes"])
    out = mail.tool_send_mail("a@b.com, c@d.fr", "Visite", "Bonjour", bcc="e@f.com")
    message, to_addrs = FakeSMTP.sent[0]
    assert to_addrs == ["a@b.com", "c@d.fr", "e@f.com"] and message["Subject"] == "Visite" and "Bcc" not in message
    assert "a@b.com" in out


def test_refusal_sends_nothing():
    login()
    state.ui = ScriptedUI(["no", ""])
    with pytest.raises(ToolError):
        mail.tool_send_mail("a@b.com", "Hi", "x")
    assert not FakeSMTP.sent


def test_unknown_domain_builds_smtp_host_from_the_address():
    assert mail.smtp_host_for("Awa@Exemple.sn") == "smtp.exemple.sn"
    assert mail.smtp_host_for("x@hotmail.fr") == "smtp-mail.outlook.com"


def test_bad_address_and_empty_password_are_refused():
    state.ui = ScriptedUI(["nope"])
    with pytest.raises(ToolError):
        mail.tool_mail_login()
    state.ui = ScriptedUI(["me@gmail.com", ""])
    with pytest.raises(ToolError):
        mail.tool_mail_login()
    assert not mail._session


def test_bad_recipient():
    login()
    state.ui = ScriptedUI(["yes"])
    with pytest.raises(ToolError):
        mail.tool_send_mail("not-an-address", "Hi", "x")


def test_refused_credentials_tell_the_user_and_leave_the_session_logged_out():
    FakeSMTP.refuse = True
    state.ui = ScriptedUI(["me@gmail.com", "wrong"])
    with pytest.raises(ToolError, match="Incorrect credentials"):
        mail.tool_mail_login()
    assert any(e[0] == "failure" and "Incorrect credentials" in e[1] for e in state.ui.events)
    assert not mail._session and len(FakeSMTP.hosts) == 1  # no pointless retry on the other port


def test_login_falls_back_to_port_465_when_587_drops_the_connection(monkeypatch):
    ports = []

    def open_(host, port, user, password):
        ports.append(port)
        if port == 587:
            raise mail.smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        return FakeSMTP(host, port)

    monkeypatch.setattr(mail, "_open", open_)
    login()
    assert ports == [587, 465] and mail._session["port"] == 465


def test_every_step_is_logged_in_the_ui_without_the_password():
    login()
    logs = [e[1] for e in state.ui.events if e[0] == "status" and e[1].startswith("[mail]")]
    assert any("connecting to smtp.gmail.com:587" in l for l in logs) and any("logged in as me@gmail.com" in l for l in logs)
    assert not any("pw" == w for e in state.ui.events for w in e[1:] if isinstance(w, str))


def test_mail_draft_opens_the_chosen_webmail_after_approval(monkeypatch):
    opened = []
    monkeypatch.setattr(mail, "_open_url", lambda url: opened.append(url) or True)
    state.ui = ScriptedUI(["yes"])
    out = mail.tool_mail_draft("a@b.com", "Hello", "Test", webmail="gmail")
    assert opened[0].startswith("https://mail.google.com/mail/?view=cm") and "NOT sent" in out
    state.ui = ScriptedUI(["yes"])
    mail.tool_mail_draft("a@b.com", "Hello", "Test", webmail="outlook")
    assert opened[1].startswith("https://outlook.live.com/mail/0/deeplink/compose?") and "subject=Hello" in opened[1]
    with pytest.raises(ToolError):
        mail.tool_mail_draft("a@b.com", "Hello", "Test", webmail="aol")


def test_web_sign_in_refuses_mailboxes_and_points_to_mail_draft():
    from coding_agent.tools import web
    with pytest.raises(ToolError, match="mail_draft"):
        web.tool_web_sign_in("https://mail.google.com")


def test_a_refused_gmail_login_offers_to_open_the_app_password_page_in_the_default_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(mail, "_open_url", lambda url: opened.append(url) or True)
    FakeSMTP.refuse = True
    state.ui = ScriptedUI(["me@gmail.com", "wrong", "yes"])
    with pytest.raises(ToolError):
        mail.tool_mail_login()
    assert opened == ["https://myaccount.google.com/apppasswords"]
    state.ui = ScriptedUI(["me@gmail.com", "wrong", "no"])
    with pytest.raises(ToolError):
        mail.tool_mail_login()
    assert len(opened) == 1
    state.ui = ScriptedUI(["me@exemple.sn", "wrong"])  # an unknown provider: nothing to open, no question
    with pytest.raises(ToolError):
        mail.tool_mail_login()
    assert len(opened) == 1


def test_a_draft_opens_gmail_when_no_address_is_known(monkeypatch):
    opened = []
    monkeypatch.setattr(mail, "_open_url", lambda url: opened.append(url) or True)
    monkeypatch.setattr(mail, "_last_address", "")
    state.ui = ScriptedUI(["yes"])
    mail.tool_mail_draft("a@b.com", "Hi", "x")
    assert opened[0].startswith("https://mail.google.com/mail/?view=cm")


def test_the_browser_is_started_detached_from_the_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(mail.sys, "platform", "linux")
    monkeypatch.setattr(mail.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(mail.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    assert mail._open_url("https://mail.google.com/x")
    cmd, kw = calls[0]
    assert cmd == ["/usr/bin/xdg-open", "https://mail.google.com/x"] and kw["start_new_session"]
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == mail.subprocess.DEVNULL
