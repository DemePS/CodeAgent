"""HTTPS behind a company proxy: the operating system's certificates, and a plain explanation."""

import ssl

from coding_agent import certificates


def test_the_system_certificate_store_is_used():
    assert certificates.use_system_certificates()  # on when the package is imported
    assert type(certificates.ssl_context()).__module__.startswith("truststore")
    assert type(ssl.create_default_context()).__module__.startswith("truststore")  # every HTTPS client


def test_it_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(certificates, "_enabled", False)
    monkeypatch.setenv("AGENT_SYSTEM_CERTS", "0")
    assert certificates.use_system_certificates() is False


def test_a_certificate_failure_is_explained_not_retried():
    error = Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate")
    text = certificates.explain(error)
    assert "company" in text and "do not retry" in text and "SSL_CERT_FILE" in text
    assert certificates.explain(Exception("timed out")) is None
