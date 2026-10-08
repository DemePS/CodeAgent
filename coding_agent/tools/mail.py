"""The send_mail tool: sends an email over SMTP, always after the person approves it."""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage

from .. import state
from ..common import ToolError

# The person's address is asked through the UI (or taken from SMTP_USER); the SMTP server is derived from its domain.
# Optional overrides (environment or .env): SMTP_HOST, SMTP_PORT (default 587; 465 = implicit TLS), SMTP_PASSWORD (else asked, without echo), MAIL_FROM.
KNOWN_SMTP_HOSTS = {
    "gmail.com": "smtp.gmail.com", "googlemail.com": "smtp.gmail.com",
    "outlook.com": "smtp-mail.outlook.com", "outlook.fr": "smtp-mail.outlook.com", "hotmail.com": "smtp-mail.outlook.com",
    "hotmail.fr": "smtp-mail.outlook.com", "live.com": "smtp-mail.outlook.com", "live.fr": "smtp-mail.outlook.com",
    "yahoo.com": "smtp.mail.yahoo.com", "yahoo.fr": "smtp.mail.yahoo.com",
    "icloud.com": "smtp.mail.me.com", "me.com": "smtp.mail.me.com",
    "orange.fr": "smtp.orange.fr", "free.fr": "smtp.free.fr", "sfr.fr": "smtp.sfr.fr",
}
_session: dict[str, str] = {}  # address and password given in this run, so the person is asked once (kept in memory only)
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


def _credentials() -> tuple[str, str, str]:
    """(address, password, host): the address from the environment or asked once; the password from the environment, else asked once."""
    user = os.environ.get("SMTP_USER") or _session.get("user")
    if not user:
        user = _ask("Your email address (the mail is sent from it): ")
        if not _ADDRESS.fullmatch(user):
            raise ToolError("A valid email address is needed to send a mail.")
    password = os.environ.get("SMTP_PASSWORD") or _session.get("password")
    if not password:
        password = _ask(f"Password for {user} (an app password for Gmail/Outlook; not shown): ", secret=True)
        if not password:
            raise ToolError("No password given; nothing was sent.")
    _session.update(user=user, password=password)
    return user, password, os.environ.get("SMTP_HOST") or smtp_host_for(user)


def _deliver(host: str, port: int, user: str, password: str, message: EmailMessage, to_addrs: list[str]) -> None:
    """One delivery attempt; every step is shown in the UI (never the password)."""
    log = state.ui.status
    context = ssl.create_default_context()
    log(f"[mail] connecting to {host}:{port} ({'TLS' if port == 465 else 'STARTTLS'})...")
    server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context) if port == 465 \
        else smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
    with server:
        log(f"[mail] connected: {host}:{port}")
        if port != 465:
            server.starttls(context=context)
            log("[mail] connection encrypted (STARTTLS)")
        if user and password:
            server.login(user, password)
            log(f"[mail] logged in as {user}")
        log(f"[mail] sending to {len(to_addrs)} recipient(s)...")
        server.send_message(message, to_addrs=to_addrs)
        log("[mail] accepted by the server")


def tool_send_mail(to: str | list, subject: str, body: str, cc: str | list = "", bcc: str | list = "") -> str:
    try:
        port = int(os.environ.get("SMTP_PORT") or 587)
    except ValueError:
        raise ToolError("SMTP_PORT must be a number.")
    recipients, copies, hidden = _addresses(to, "to"), _addresses(cc, "cc"), _addresses(bcc, "bcc")
    if not recipients:
        raise ToolError("At least one recipient is needed in 'to'.")
    if not subject.strip():
        raise ToolError("The subject is empty.")
    if "\n" in subject or "\r" in subject:
        raise ToolError("The subject must be a single line.")

    user, password, host = _credentials()
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
    ports = [port] if os.environ.get("SMTP_PORT") or port == 465 else [587, 465]  # no port chosen: 587 (STARTTLS), then 465 (TLS)
    for attempt, port in enumerate(ports):
        try:
            _deliver(host, port, user, password, message, recipients + copies + hidden)
            break
        except smtplib.SMTPAuthenticationError as e:
            state.ui.warning(f"[mail] login refused by {host}: {e.smtp_code} {e.smtp_error!r}")
            _session.pop("password", None)  # asked again at the next try
            raise ToolError(f"{host} refused the login for {user} ({e.smtp_code}). Gmail/Outlook need an app password, not the usual one; nothing was sent.")
        except (smtplib.SMTPException, OSError) as e:
            state.ui.warning(f"[mail] {host}:{port} failed: {type(e).__name__}: {e}")
            if attempt + 1 < len(ports):
                state.ui.status(f"[mail] trying port {ports[attempt + 1]} instead.")
                continue
            raise ToolError(f"The email could not be sent through {host}:{port}: {e}. Check that {host} is the right outgoing server for {user} "
                            "(SMTP_HOST overrides it) and that the network allows SMTP.")
    return f"Email sent to {', '.join(recipients + copies + hidden)} (subject: {subject})."
