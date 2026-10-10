"""Plain-language explanations of why a call to Claude failed, with what to check."""

from __future__ import annotations

import os
import re

import anthropic

from .config import SettingError, current_api_key, get_model, key_location, model_setting, uses_anthropic_api, uses_deepseek

_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}")


def redact(text: str) -> str:
    """Hide any Anthropic API key in a text: the key in use, and anything shaped like one."""
    key = current_api_key()
    if key:
        text = text.replace(key, "[api key hidden]")
    return _KEY_PATTERN.sub("[api key hidden]", text)


def endpoint() -> str:
    if uses_deepseek():
        return "the DeepSeek API"
    if uses_anthropic_api():
        return "the Anthropic API"
    return os.environ.get("ANTHROPIC_FOUNDRY_ENDPOINT") or "(ANTHROPIC_FOUNDRY_ENDPOINT is not set)"


def connection_summary() -> str:
    """Which deployment, where, and how the agent signs in -- shown at startup."""
    if uses_anthropic_api():
        return f"model {get_model()} on {endpoint()}, API key"
    auth = "API key" if os.environ.get("ANTHROPIC_FOUNDRY_API_KEY") else "Microsoft sign-in (Azure AD)"
    return f"deployment {get_model()} at {endpoint()}, {auth}"


def root_cause(error: BaseException) -> str:
    """The innermost exception, e.g. the TLS or DNS error behind an APIConnectionError."""
    while error.__cause__ is not None or error.__context__ is not None:
        error = error.__cause__ or error.__context__
    text = redact(str(error) or type(error).__name__)
    return text if len(text) <= 300 else text[:300] + " …"


def describe(error: BaseException) -> str | None:
    """Explain a failed call to Claude; None if the error is not about reaching Claude."""
    if isinstance(error, SettingError):  # CODEAGENT_THINKING / CODEAGENT_EFFORT: the message is already meant for the person
        return str(error)
    if isinstance(error, anthropic.APIStatusError):
        code, detail = error.status_code, redact(str(error.message))[:300]
        if code in (401, 403):
            if uses_anthropic_api():
                return f"Access denied by {endpoint()} (HTTP {code}): check {key_location()}. Details: {detail}"
            if os.environ.get("ANTHROPIC_FOUNDRY_API_KEY"):
                return f"Access denied by {endpoint()} (HTTP {code}): check the API key (ANTHROPIC_FOUNDRY_API_KEY). Details: {detail}"
            return (f"Access denied by {endpoint()} (HTTP {code}): you are signed in, but your account has no "
                    "access to the Claude service. Ask IT for access (a role such as 'Azure AI User' on the "
                    f"Foundry resource). Details: {detail}")
        if code == 404:
            if uses_anthropic_api():
                return (f"Not found (HTTP 404): no model named '{get_model()}' on {endpoint()}. Check {model_setting()} "
                        f"(a model ID such as claude-sonnet-5). Details: {detail}")
            return (f"Not found (HTTP 404): no deployment named '{get_model()}' at {endpoint()}. Check "
                    "ANTHROPIC_FOUNDRY_DEPLOYMENT (the deployment name in Foundry) and that the endpoint "
                    f"ends with /anthropic. Details: {detail}")
        if code in (408, 504):
            return (f"Claude did not answer in time (HTTP {code}, from {endpoint()}). The deployment '{get_model()}' "
                    "may be overloaded or not fully deployed; check it in the Foundry portal, retry, and run "
                    f"`coding-agent --check` to see which step times out. Details: {detail}")
        if code == 400 and "credit balance" in detail.lower():
            return (f"Your Anthropic account has no credit left (HTTP 400): add credit at console.anthropic.com "
                    f"(Plans & Billing), then retry. Details: {detail}")
        if code == 529:
            return f"Claude is overloaded right now (HTTP 529); retry in a moment. Details: {detail}"
        if code == 429:
            limit = "the account's rate limit" if uses_anthropic_api() else "the deployment's rate limit"
            return f"Too many requests (HTTP 429): {limit} is reached; wait a minute and retry. Details: {detail}"
        if code >= 500:
            return f"The Claude service had a problem (HTTP {code}); retry in a moment. Details: {detail}"
        return f"The request was refused (HTTP {code}): {detail}"
    if isinstance(error, anthropic.APIConnectionError):
        cause = root_cause(error)
        hint = (" Your network seems to inspect TLS traffic (a company proxy): its certificate must be trusted "
                "by Python." if "CERTIFICATE_VERIFY_FAILED" in cause or "certificate" in cause.lower() else
                " Check the endpoint, your network, VPN or proxy.")
        return f"Could not reach {endpoint()} (network error: {cause}).{hint}"
    try:
        from azure.core.exceptions import ClientAuthenticationError
    except ImportError:
        ClientAuthenticationError = ()  # noqa: N806
    if ClientAuthenticationError and isinstance(error, ClientAuthenticationError):
        from .signin import browser_sign_in_allowed
        first = (str(error).splitlines() or [""])[0][:300]
        if browser_sign_in_allowed():
            return ("Microsoft sign-in did not complete. Retry and sign in with your work account on the "
                    "Microsoft page that opens in your browser; if it keeps failing, contact IT (your account "
                    "may need access to the Claude service). Details: " + first)
        return ("Microsoft sign-in failed: no signed-in account was found (sign-in page disabled by "
                "ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN=0). Run `az login`, or set ANTHROPIC_FOUNDRY_API_KEY. "
                "Details: " + first)
    if isinstance(error, KeyError) and error.args == ("ANTHROPIC_FOUNDRY_ENDPOINT",):
        return ("ANTHROPIC_FOUNDRY_ENDPOINT is not set (the Foundry endpoint, ending with /anthropic). "
                "Or set ANTHROPIC_API_KEY to use Anthropic's API directly.")
    return None
