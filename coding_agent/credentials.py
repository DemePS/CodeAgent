"""Logins typed by the person (mail, SMS), kept in ~/.coding-agent/secrets so they are asked only once.

One JSON file per service, readable by the owner only (folder 0700, files 0600), in clear text: anyone who can read the
person's home folder can read them. `coding-agent --forget-secrets` deletes them; a login the server refuses is deleted at once.
The folder can be moved with AGENT_SECRETS_DIR. Nothing is ever sent to Claude."""

import json
import os
import re
from pathlib import Path


def secrets_dir() -> Path:
    return Path(os.environ.get("AGENT_SECRETS_DIR") or Path.home() / ".coding-agent" / "secrets").expanduser()


def _file(name: str) -> Path:
    return secrets_dir() / f"{re.sub(r'[^a-z0-9_-]', '_', name.lower())}.json"


def load(name: str) -> dict:
    try:
        data = json.loads(_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(name: str, values: dict) -> Path:
    path = _file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        json.dump(values, f)
    try:
        path.chmod(0o600)  # a file that already existed keeps its old mode otherwise
    except OSError:
        pass
    return path


def forget(name: str | None = None) -> list[str]:
    """Delete one service's login, or all of them; returns what was removed."""
    paths = [_file(name)] if name else sorted(secrets_dir().glob("*.json"))
    removed = []
    for path in paths:
        try:
            path.unlink()
            removed.append(path.stem)
        except OSError:
            pass
    return removed
