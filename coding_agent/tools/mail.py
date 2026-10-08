"""The mail_login and send_mail tools: log in to the person's mailbox over SMTP, then send emails, each after the person approves it."""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage

from .. import state
from ..common import ToolError

# mail_login asks the person for their address and password through the UI (or takes SMTP_USER / SMTP_PASSWORD) and derives the SMTP
# server from the address's domain. send_mail never asks: it needs a successful mail_login first.
# Optional overrides (environment or .env): SMTP_HOST, SMTP_PORT (default 587, then 465; 465 = implicit TLS), MAIL_FROM.
KNOWN_SMTP_HOSTS = {
    "gmail.com": "smtp.gmail.com", "googlemail.com": "smtp.gmail.com",
    "outlook.com": "smtp-mail.outlook.com", "outlook.fr": "smtp-mail.outlook.com", "hotmail.com": "smtp-mail.outlook.com",
    "hotmail.fr": "smtp-mail.outlook.com", "live.com": "smtp-mail.outlook.com", "live.fr": "smtp-mail.outlook.com",
    "yahoo.com": "smtp.mail.yahoo.com", "yahoo.fr": "smtp.mail.yahoo.com",
    "icloud.com": "smtp.mail.me.com", "me.com": "smtp.mail.me.com",
    "orange.fr": "smtp.orange.fr", "free.fr": "smtp.free.fr", "sfr.fr": "smtp.sfr.fr",
}
_session: dict = {}  # the verified login of this run: user, password, host, port (kept in memory only)
_ADDRESS = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")
SMTP_TIMEOUT_SECONDS = 30


def _addresses(value: str | list, field: str) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[,;]", value or "")
    found = [a.strip() for a in items if a and a.strip()]
    for a in found:
        if not _ADDRESS.fullmatch(a):
            raise ToolError(f"{field}: '{a}' is not a valid email address.")
    return found


def smtp_host_for(address: str) -> str:
    """The outgoing server of an address: a well-known provider, else smtp.<domain>."""
    domain = address.rsplit("@", 1)[1].lower()
    return KNOWN_SMTP_HOSTS.get(domain, f"smtp.{domain}")


def _ask(prompt: str, secret: bool = False) -> str:
    ask = getattr(state.ui, "ask_secret", None) if secret else None
    return (ask or state.ui.ask_text)(prompt).strip()


def _ports() -> list[int]:
    chosen = os.environ.get("SMTP_PORT")
    if not chosen:
        return [587, 465]  # STARTTLS, then implicit TLS
    try:
        return [int(chosen)]
    except ValueError:
        raise ToolError("SMTP_PORT must be a number.")


def _open(host: str, port: int, user: str, password: str) -> smtplib.SMTP:
    """A connected, encrypted and logged-in connection; every step is shown in the UI (never the password)."""
    log = state.ui.status
    context = ssl.create_default_context()
    log(f"[mail] connecting to {host}:{port} ({'TLS' if port == 465 else 'STARTTLS'})...")
    server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context) if port == 465 \
        else smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
    try:
        log(f"[mail] connected: {host}:{port}")
        if port != 465:
            server.starttls(context=context)
            log("[mail] connection encrypted (STARTTLS)")
        server.ehlo_or_helo_if_needed()
        if "PLAIN" in server.esmtp_features.get("auth", "").split():
            # After a refused AUTH PLAIN, smtplib tries AUTH LOGIN and Gmail then drops the connection: the person would read
            # "connection unexpectedly closed" instead of "bad credentials". PLAIN only keeps the real answer.
            server.esmtp_features["auth"] = "PLAIN"
        server.login(user, password)
        log(f"[mail] logged in as {user}")
    except BaseException:
        server.close()
        raise
    return server


def _incorrect_credentials(host: str, user: str, code: int) -> ToolError:
    state.ui.failure(f"Incorrect credentials: {host} refused the login for {user}. Check the address and the password "
                     "(Gmail and Outlook need an app password, not the usual one).")
    return ToolError(f"Incorrect credentials: {host} refused the login for {user} ({code}). Tell the user; do not retry with the same password. "
                     "Gmail/Outlook need an app password, not the usual one. Nothing was sent. Call mail_login again to let the user retry.")


def tool_mail_login() -> str:
    """Ask the person for their address and password, check them against their SMTP server, and keep them for send_mail."""
    _session.clear()
    user = os.environ.get("SMTP_USER") or _ask("Your email address (the mail is sent from it): ")
    if not _ADDRESS.fullmatch(user):
        raise ToolError("A valid email address is needed to log in.")
    password = os.environ.get("SMTP_PASSWORD") or _ask(f"Password for {user} (an app password for Gmail/Outlook; not shown): ", secret=True)
    if not password:
        raise ToolError("No password given; not logged in.")
    host = os.environ.get("SMTP_HOST") or smtp_host_for(user)
    ports = _ports()
    for attempt, port in enumerate(ports):
        try:
            _open(host, port, user, password).quit()
            break
        except smtplib.SMTPAuthenticationError as e:
            raise _incorrect_credentials(host, user, e.smtp_code)
        except (smtplib.SMTPException, OSError) as e:
            state.ui.warning(f"[mail] {host}:{port} failed: {type(e).__name__}: {e}")
            if attempt + 1 < len(ports):
                state.ui.status(f"[mail] trying port {ports[attempt + 1]} instead.")
                continue
            raise ToolError(f"Could not log in through {host}:{port}: {e}. Check that {host} is the right outgoing server for {user} "
                            "(SMTP_HOST overrides it) and that the network allows SMTP.")
    _session.update(user=user, password=password, host=host, port=port)
    state.ui.success(f"Logged in as {user} ({host}:{port}).")
    return f"Logged in as {user} through {host}:{port}. send_mail can now be used."


def tool_send_mail(to: str | list, subject: str, body: str, cc: str | list = "", bcc: str | list = "") -> str:
    if not _session:
        raise ToolError("Not logged in to a mailbox: call mail_login first (it asks the user for their address and password).")
    recipients, copies, hidden = _addresses(to, "to"), _addresses(cc, "cc"), _addresses(bcc, "bcc")
    if not recipients:
        raise ToolError("At least one recipient is needed in 'to'.")
    if not subject.strip():
        raise ToolError("The subject is empty.")
    if "\n" in subject or "\r" in subject:
        raise ToolError("The subject must be a single line.")

    user, password, host, port = (_session[k] for k in ("user", "password", "host", "port"))
    sender = os.environ.get("MAIL_FROM") or user
    state.ui.panel("Send an email", [f"From:    {sender}  (via {host})", f"To:      {', '.join(recipients)}",
                                     *([f"Cc:      {', '.join(copies)}"] if copies else []),
                                     *([f"Bcc:     {', '.join(hidden)}"] if hidden else []),
                                     f"Subject: {subject}", "", *body.splitlines()], tone="network")
    if state.auto_mode:
        state.ui.status("(autonomous mode: sending an email still needs your approval)")
    if state.ui.confirm("Send?", ("yes", "no")) != "yes":
        feedback = state.ui.ask_text("Why not? (optional): ")
        raise ToolError("The user refused; nothing was sent." + (f" User feedback: {feedback}" if feedback else ""))

    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, ", ".join(recipients), subject
    if copies:
        message["Cc"] = ", ".join(copies)
    message.set_content(body)
    try:
        with _open(host, port, user, password) as server:
            state.ui.status(f"[mail] sending to {len(recipients + copies + hidden)} recipient(s)...")
            server.send_message(message, to_addrs=recipients + copies + hidden)
            state.ui.status("[mail] accepted by the server")
    except smtplib.SMTPAuthenticationError as e:
        _session.clear()
        raise _incorrect_credentials(host, user, e.smtp_code)
    except (smtplib.SMTPException, OSError) as e:
        state.ui.warning(f"[mail] {host}:{port} failed: {type(e).__name__}: {e}")
        raise ToolError(f"The email could not be sent through {host}:{port}: {e}")
    return f"Email sent to {', '.join(recipients + copies + hidden)} (subject: {subject})."
