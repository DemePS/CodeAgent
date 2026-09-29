"""Microsoft sign-in (Azure AD / Entra ID) when no API key is set.

In order:
1. Without asking anything:
   - with your organization's app registration (ANTHROPIC_FOUNDRY_CLIENT_ID, e.g. to call an API
     Management gateway): the account signed into Windows, through the Windows broker;
   - otherwise what DefaultAzureCredential finds: the account signed into Windows (Windows
     broker, package azure-identity-broker), a developer's `az login`, Visual Studio Code,
     environment variables, a managed identity...
2. Otherwise the Microsoft sign-in page, opened in the browser once. The account is remembered
   (~/.coding-agent/azure-account*.json, no secret in it) and its tokens are kept in the OS's
   encrypted token cache on Windows and macOS, so the next launches sign in silently.

Environment variables: AZURE_TENANT_ID (the tenant to sign in to, when it is not the account's home
tenant), ANTHROPIC_FOUNDRY_CLIENT_ID (your organization's app registration, a public client; default:
Microsoft's public Azure CLI client), TOKEN_SCOPE (what the token is for; default Foundry:
https://ai.azure.com/.default), ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN=0 to never open the sign-in page
(servers, CI).
"""

from __future__ import annotations

import os
import sys
import threading

ACCOUNT_FILE_NAME = "azure-account.json"  # with an app registration: azure-account-<client id>.json


def client_id() -> str | None:
    return os.environ.get("ANTHROPIC_FOUNDRY_CLIENT_ID") or None


def windows_account(client: str):
    """The account signed into Windows, for the app registration `client` (None if unavailable)."""
    if sys.platform != "win32":
        return None
    try:
        from azure.identity.broker import InteractiveBrowserBrokerCredential
    except ImportError:
        return None
    options = {"tenant_id": os.environ["AZURE_TENANT_ID"]} if os.environ.get("AZURE_TENANT_ID") else {}
    # parent_window_handle 0: no window to attach a prompt to -- only the silent default account is used.
    return InteractiveBrowserBrokerCredential(client_id=client, use_default_broker_account=True,
                                              parent_window_handle=0, **options)


def browser_sign_in_allowed() -> bool:
    return os.environ.get("ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN", "1").strip().lower() not in ("0", "false", "no", "off")


class SignIn:
    """A token credential (get_token) for get_bearer_token_provider. Thread-safe: one sign-in page
    at most, even when the agent and the memory curator ask for a token at the same time."""

    def __init__(self) -> None:
        from azure.identity import DefaultAzureCredential

        from .config import AGENT_HOME

        self._lock = threading.Lock()
        client = client_id()
        # The account remembered for this app registration (one registration's sign-in is not another's).
        self._account_file = AGENT_HOME / (f"azure-account-{client}.json" if client else ACCOUNT_FILE_NAME)
        if client:
            broker = windows_account(client)
            self._silent = [broker] if broker else []
        else:
            self._silent = [DefaultAzureCredential()]  # never opens a page (interactive browser excluded)
        self._browser = None
        # Signed in through the page before: go straight to the remembered account.
        self._use_browser = browser_sign_in_allowed() and self._account_file.is_file()

    def get_token(self, *scopes: str, **kwargs):
        from azure.core.exceptions import ClientAuthenticationError

        with self._lock:
            if self._use_browser:
                return self._browser_token(scopes, kwargs)
            failure: Exception = ClientAuthenticationError("No signed-in account was found.")
            for credential in self._silent:
                try:
                    return credential.get_token(*scopes, **kwargs)
                except Exception as error:  # unavailable, or it needs a prompt: next way
                    failure = error
            if not browser_sign_in_allowed():
                raise failure if isinstance(failure, ClientAuthenticationError) else ClientAuthenticationError(str(failure))
            self._use_browser = True
            return self._browser_token(scopes, kwargs)

    def _browser_token(self, scopes, kwargs):
        from azure.identity import AuthenticationRecord, InteractiveBrowserCredential, TokenCachePersistenceOptions

        if self._browser is None:
            record = None
            try:
                record = AuthenticationRecord.deserialize(self._account_file.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            options = {}
            if os.environ.get("AZURE_TENANT_ID"):
                options["tenant_id"] = os.environ["AZURE_TENANT_ID"]
            if client_id():
                options["client_id"] = client_id()
            if sys.platform in ("win32", "darwin"):  # encrypted by the OS (DPAPI, Keychain)
                options["cache_persistence_options"] = TokenCachePersistenceOptions(name="coding-agent")
            self._browser = InteractiveBrowserCredential(authentication_record=record, **options)
            if record is None:
                # First sign-in: the page opens; remember the account for the next launches.
                record = self._browser.authenticate(scopes=list(scopes))
                try:
                    self._account_file.parent.mkdir(parents=True, exist_ok=True)
                    self._account_file.write_text(record.serialize(), encoding="utf-8")
                except OSError:
                    pass  # signed in anyway; the page opens again next time
        return self._browser.get_token(*scopes, **kwargs)


DEFAULT_SCOPE = "https://ai.azure.com/.default"
_shared: SignIn | None = None
_shared_lock = threading.Lock()


def shared() -> SignIn:
    """The one sign-in of this process: Claude's client and the application (e.g. an access check)
    use the same, so there is never more than one sign-in page."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = SignIn()
        return _shared


def access_token(scope: str | None = None) -> str:
    """An access token for `scope` (default: TOKEN_SCOPE, else Foundry), signing in if needed."""
    return shared().get_token(scope or os.environ.get("TOKEN_SCOPE") or DEFAULT_SCOPE).token

