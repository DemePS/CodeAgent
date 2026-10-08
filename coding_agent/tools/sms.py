"""The send_sms tool: sends a text message through Twilio, always after the person approves it."""

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .. import state
from ..common import ToolError

# Twilio account: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM (a Twilio number, E.164) from the environment (or .env);
# whatever is missing is asked through the UI, once per run, and kept in memory only.
_FIELDS = (("TWILIO_ACCOUNT_SID", "Twilio Account SID (starts with AC): ", False),
           ("TWILIO_AUTH_TOKEN", "Twilio Auth Token (not shown): ", True),
           ("TWILIO_FROM", "Your Twilio phone number (international format, e.g. +15551234567): ", False))
_session: dict[str, str] = {}
_PHONE = re.compile(r"\+[1-9]\d{6,14}")
SMS_TIMEOUT_SECONDS = 30
SMS_MAX_CHARACTERS = 1600


def _credentials() -> tuple[str, str, str]:
    values = []
    for key, prompt, secret in _FIELDS:
        value = os.environ.get(key) or _session.get(key)
        if not value:
            ask = getattr(state.ui, "ask_secret", None) if secret else None
            value = (ask or state.ui.ask_text)(prompt).strip()
            if not value:
                raise ToolError(f"{key} is needed to send an SMS; nothing was sent.")
        values.append(value)
    if not _PHONE.fullmatch(values[2]):
        raise ToolError("TWILIO_FROM must be a phone number in international format, e.g. +15551234567.")
    _session.update(zip((k for k, _, _ in _FIELDS), values))
    return tuple(values)


def tool_send_sms(to: str, body: str) -> str:
    to = re.sub(r"[ .\-()]", "", to or "")
    if not _PHONE.fullmatch(to):
        raise ToolError("'to' must be a phone number in international format, e.g. +33612345678.")
    if not body.strip():
        raise ToolError("The message is empty.")
    if len(body) > SMS_MAX_CHARACTERS:
        raise ToolError(f"The message is too long ({len(body)} characters, maximum {SMS_MAX_CHARACTERS}).")

    sid, token, sender = _credentials()
    state.ui.panel("Send an SMS", [f"From: {sender}", f"To:   {to}", "", *body.splitlines()], tone="network")
    if state.auto_mode:
        state.ui.status("(autonomous mode: sending an SMS still needs your approval)")
    if state.ui.confirm("Send?", ("yes", "no")) != "yes":
        feedback = state.ui.ask_text("Why not? (optional): ")
        raise ToolError("The user refused; nothing was sent." + (f" User feedback: {feedback}" if feedback else ""))

    request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json",
        data=urllib.parse.urlencode({"To": to, "From": sender, "Body": body}).encode(),
        headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()})
    try:
        with urllib.request.urlopen(request, timeout=SMS_TIMEOUT_SECONDS) as response:
            sent = json.load(response)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("message", e.reason)
        except Exception:
            detail = e.reason
        raise ToolError(f"The SMS could not be sent: {detail}")
    except OSError as e:
        raise ToolError(f"The SMS could not be sent: {e}")
    return f"SMS sent to {to} (id {sent.get('sid', '?')}, status {sent.get('status', '?')})."
