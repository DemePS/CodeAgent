"""Microsoft sign-in (Azure AD / Entra ID) when no API key is set.

In order:
1. What DefaultAzureCredential finds without asking anything: on a company Windows PC the account
   signed into Windows (through the Windows broker, package azure-identity-broker), a developer's
   `az login`, Visual Studio Code, environment variables, a managed identity...
2. Otherwise the Microsoft sign-in page, opened in the browser once. The account is remembered
   (~/.coding-agent/azure-account.json, no secret in it) and its tokens are kept in the OS's
   encrypted token cache on Windows and macOS, so the next launches sign in silently.

Environment variables: AZURE_TENANT_ID (the Foundry resource's tenant, when it is not the account's
home tenant), AZURE_CLIENT_ID (an app registration of your organization for the sign-in page;
default: Microsoft's public Azure CLI client), ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN=0 to never open
the sign-in page (servers, CI).
"""

from __future__ import annotations

import os
import sys
import threading

ACCOUNT_FILE_NAME = "azure-account.json"


def browser_sign_in_allowed() -> bool:
    return os.environ.get("ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN", "1").strip().lower() not in ("0", "false", "no", "off")


class SignIn:
    """A token credential (get_token) for get_bearer_token_provider. Thread-safe: one sign-in page
    at most, even when the agent and the memory curator ask for a token at the same time."""

    def __init__(self) -> None:
        from azure.identity import DefaultAzureCredential

        from .config import AGENT_HOME

        self._lock = threading.Lock()
        self._account_file = AGENT_HOME / ACCOUNT_FILE_NAME
        self._default = DefaultAzureCredential()  # never opens a page (interactive browser excluded)
        self._browser = None
        # Signed in through the page before: go straight to the remembered account.
        self._use_browser = browser_sign_in_allowed() and self._account_file.is_file()

    def get_token(self, *scopes: str, **kwargs):
        from azure.core.exceptions import ClientAuthenticationError

        with self._lock:
            if self._use_browser:
                return self._browser_token(scopes, kwargs)
            try:
                return self._default.get_token(*scopes, **kwargs)
            except ClientAuthenticationError:
                if not browser_sign_in_allowed():
                    raise
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
            if os.environ.get("AZURE_CLIENT_ID"):
                options["client_id"] = os.environ["AZURE_CLIENT_ID"]
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
