from functools import lru_cache

from anthropic import AnthropicFoundry

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
import os

from dotenv import load_dotenv
load_dotenv()

@lru_cache(maxsize=1)
def _get_client() -> AnthropicFoundry:
    api_key = os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
    if api_key:
        return AnthropicFoundry(
            api_key=api_key,
            base_url=os.environ["ANTHROPIC_FOUNDRY_ENDPOINT"],
            max_retries=2,
        )
    scope = os.environ.get("TOKEN_SCOPE", "https://ai.azure.com/.default")
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), scope)
    return AnthropicFoundry(
        azure_ad_token_provider=token_provider,
        base_url=os.environ["ANTHROPIC_FOUNDRY_ENDPOINT"],
        max_retries=2,
    )
