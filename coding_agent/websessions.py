"""Sign-ins kept for the browsing tools: the cookies a site gave the person after they signed in themselves.

The person types the password in a visible browser window (web_sign_in); the agent never sees it, and
only the resulting session (cookies, local storage) is saved, one file per site in
~/.coding-agent/web-sessions, readable by the owner only. A saved session is as good as a password until
it expires: files unused for WEB_SESSION_DAYS are deleted, and `coding-agent --forget-logins` removes them.

A site that signs in with a token instead (web_set_token) is kept the same way: one `<site>.token` file with the header name and
the token the person typed, sent only to that site. It expires and is removed with the sessions.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .config import AGENT_HOME

SESSIONS_DIR = AGENT_HOME / "web-sessions"
WEB_SESSION_DAYS = int(os.environ.get("AGENT_WEB_SESSION_DAYS") or 30)


def site_name(host: str) -> str:
    """www.x.com and x.com are one site; a file name that cannot escape the folder."""
    host = host.lower().removeprefix("www.")
    return re.sub(r"[^a-z0-9.-]", "_", host)[:100] or "site"


def _file(host: str) -> Path:
    return SESSIONS_DIR / f"{site_name(host)}.json"


def save(host: str, storage: dict) -> Path:
    """Keep the session the browser reports (Playwright's storage_state), for this site only."""
    path = _file(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"cookies": storage.get("cookies", []), "origins": storage.get("origins", [])})
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        f.write(data)
    try:
        path.chmod(0o600)  # a file that already existed keeps its old mode otherwise
    except OSError:
        pass
    return path


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        f.write(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save_token(host: str, header: str, value: str) -> Path:
    """Keep a header (name and token) to send to this site only."""
    path = SESSIONS_DIR / f"{site_name(host)}.token"
    _write_private(path, json.dumps({"header": header, "value": value}))
    return path


def load_tokens() -> dict[str, tuple[str, str]]:
    """Site -> (header name, token). Tokens not renewed for WEB_SESSION_DAYS are deleted."""
    tokens: dict[str, tuple[str, str]] = {}
    for path in sorted(SESSIONS_DIR.glob("*.token")) if SESSIONS_DIR.is_dir() else []:
        try:
            if time.time() - path.stat().st_mtime > WEB_SESSION_DAYS * 86400:
                path.unlink()
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            tokens[path.stem] = (str(data["header"]), str(data["value"]))
        except (OSError, ValueError, KeyError):
            continue
    return tokens


def load_all() -> dict | None:
    """Every saved session, as one storage state for a new browser context (None: nothing saved).
    Sessions not renewed for WEB_SESSION_DAYS are deleted."""
    if not SESSIONS_DIR.is_dir():
        return None
    cookies, origins = [], []
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            if time.time() - path.stat().st_mtime > WEB_SESSION_DAYS * 86400:
                path.unlink()
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cookies += data.get("cookies", [])
        origins += data.get("origins", [])
    return {"cookies": cookies, "origins": origins} if cookies or origins else None


def sites() -> list[str]:
    if not SESSIONS_DIR.is_dir():
        return []
    return sorted({p.stem for p in SESSIONS_DIR.glob("*.json")} | {p.stem for p in SESSIONS_DIR.glob("*.token")})


def forget(host: str | None = None) -> list[str]:
    """Delete one site's saved session, or all of them; returns the sites removed."""
    names = [site_name(host)] if host and host != "all" else sites()
    removed = []
    for name in names:
        found = False
        for suffix in (".json", ".token"):
            path = SESSIONS_DIR / f"{name}{suffix}"
            if path.is_file():
                path.unlink()
                found = True
        if found:
            removed.append(name)
    return removed
