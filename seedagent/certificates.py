"""HTTPS through a company proxy: trust the certificates the operating system trusts.

Many company networks inspect HTTPS: a proxy re-signs every site with the company's own root
certificate, installed in the Windows (or macOS) certificate store -- which is why the browser works.
Python brings its own list of certificates (certifi) and does not know that root, so every HTTPS call
fails with CERTIFICATE_VERIFY_FAILED. (uv's --system-certs / UV_SYSTEM_CERTS only covers uv's own
downloads: the packages it installs, not the program it runs.)

With truststore, Python checks certificates with the operating system instead, like the browser:
every HTTPS connection of the agent -- Claude, download_file, the gateway -- trusts what Windows
trusts. It is turned on when the package is imported; AGENT_SYSTEM_CERTS=0 turns it off.
"""

from __future__ import annotations

import os
import ssl

_enabled = False


def use_system_certificates() -> bool:
    """Make Python's ssl use the operating system's certificate store (once). True if it is on."""
    global _enabled
    if _enabled:
        return True
    if (os.environ.get("AGENT_SYSTEM_CERTS") or "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        import truststore
    except ImportError:  # not installed: Python's own certificates, as before
        return False
    truststore.inject_into_ssl()
    _enabled = True
    return True


def ssl_context() -> ssl.SSLContext:
    """A client context that checks certificates with the operating system when it can."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


def explain(error: BaseException) -> str | None:
    """A plain explanation for a certificate failure, None for any other error."""
    if "CERTIFICATE_VERIFY_FAILED" not in str(error) and "certificate verify failed" not in str(error).lower():
        return None
    return ("the site's HTTPS certificate is not trusted on this machine. On a company network this "
            "usually means a proxy inspects HTTPS with the company's own certificate: this is not a "
            "temporary error, do not retry. The agent trusts the operating system's certificates when "
            "truststore is installed (uv sync) and AGENT_SYSTEM_CERTS is not 0; otherwise ask IT for the "
            "company root certificate and set SSL_CERT_FILE to it.")
