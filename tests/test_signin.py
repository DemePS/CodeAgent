"""Microsoft sign-in: silent credentials first, then the sign-in page once, then the remembered account."""

import threading

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AuthenticationRecord

from coding_agent import config, errors, signin

SCOPE = "https://ai.azure.com/.default"


class FakeDefault:
    works = False

    def __init__(self, **kwargs):
        pass

    def get_token(self, *scopes, **kwargs):
        if not FakeDefault.works:
            raise ClientAuthenticationError("DefaultAzureCredential failed to retrieve a token")
        return AccessToken("silent-token", 9999999999)


class FakeBrowser:
    pages = 0  # sign-in pages opened
    created: list[dict] = []

    def __init__(self, authentication_record=None, **options):
        self.record = authentication_record
        FakeBrowser.created.append(options)

    def authenticate(self, scopes):
        FakeBrowser.pages += 1
        return AuthenticationRecord("tenant", "client", "login.microsoftonline.com", "home-id", "ada@corp.example")

    def get_token(self, *scopes, **kwargs):
        return AccessToken("browser-token", 9999999999)


@pytest.fixture(autouse=True)
def fakes(tmp_path, monkeypatch):
    import azure.identity
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", FakeDefault)
    monkeypatch.setattr(azure.identity, "InteractiveBrowserCredential", FakeBrowser)
    monkeypatch.setattr(config, "AGENT_HOME", tmp_path / "agent-home")
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN", raising=False)
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
    FakeDefault.works, FakeBrowser.pages, FakeBrowser.created = False, 0, []
    monkeypatch.setattr(signin, "_shared", None)
    return tmp_path / "agent-home" / signin.ACCOUNT_FILE_NAME


def test_a_signed_in_account_is_used_silently(fakes):
    FakeDefault.works = True
    assert signin.SignIn().get_token(SCOPE).token == "silent-token"
    assert FakeBrowser.pages == 0 and not fakes.exists()


def test_otherwise_the_sign_in_page_opens_once_and_the_account_is_remembered(fakes):
    credential = signin.SignIn()
    assert credential.get_token(SCOPE).token == "browser-token"
    assert credential.get_token(SCOPE).token == "browser-token"
    assert FakeBrowser.pages == 1
    assert "ada@corp.example" in fakes.read_text()
    # Next launch: straight to the remembered account, no page.
    assert signin.SignIn().get_token(SCOPE).token == "browser-token"
    assert FakeBrowser.pages == 1 and FakeBrowser.created[-1] is not None


def test_one_page_at_most_when_two_threads_ask_at_once(fakes):
    credential = signin.SignIn()
    threads = [threading.Thread(target=credential.get_token, args=(SCOPE,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert FakeBrowser.pages == 1


def test_tenant_and_client_come_from_the_environment(fakes, monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "corp-tenant")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_CLIENT_ID", "corp-app")
    signin.SignIn().get_token(SCOPE)
    assert FakeBrowser.created[0]["tenant_id"] == "corp-tenant" and FakeBrowser.created[0]["client_id"] == "corp-app"


def test_the_page_can_be_disabled(fakes, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN", "0")
    with pytest.raises(ClientAuthenticationError) as caught:
        signin.SignIn().get_token(SCOPE)
    assert FakeBrowser.pages == 0
    assert "az login" in errors.describe(caught.value)


def test_a_failed_sign_in_is_explained_in_plain_words(fakes):
    message = errors.describe(ClientAuthenticationError("User cancelled the sign-in"))
    assert "work account" in message and "User cancelled" in message and "az login" not in message


def test_with_an_app_registration_its_own_sign_in_is_used(fakes, monkeypatch):
    # e.g. an API Management gateway's API: `az login` and co. cannot get tokens for it, so they are
    # skipped; off Windows (no Windows account broker) the sign-in page is used.
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_CLIENT_ID", "corp-app")
    FakeDefault.works = True
    assert signin.SignIn().get_token("api://gateway/.default").token == "browser-token"
    assert FakeBrowser.created[0]["client_id"] == "corp-app"
    assert (fakes.parent / "azure-account-corp-app.json").is_file() and not fakes.exists()


def test_extra_headers_go_with_every_request(monkeypatch):
    import httpx
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "x", "content": [],
                                         "stop_reason": "end_turn", "stop_sequence": None,
                                         "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_ENDPOINT", "https://gateway.example/excel-filler")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k")
    monkeypatch.setattr(config, "CLIENT_HEADERS", {"x-app-version": "1.2.3"})
    config._get_client.cache_clear()
    try:
        client = config._get_client().with_options(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        client.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "hi"}])
    finally:
        config._get_client.cache_clear()
    assert seen["x-app-version"] == "1.2.3"


def test_one_shared_sign_in_for_claude_and_the_application(fakes, monkeypatch):
    monkeypatch.setenv("TOKEN_SCOPE", "api://gateway/.default")
    assert signin.shared() is signin.shared()
    assert signin.access_token() == "browser-token"
    assert signin.access_token() == "browser-token"
    assert FakeBrowser.pages == 1
