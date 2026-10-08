"""Configuration: environment variables (or a .env file), limits, paths and the Claude client."""

import os
import re
import shutil
import sys
import threading
from pathlib import Path

from anthropic import Anthropic, AnthropicFoundry
from dotenv import find_dotenv, load_dotenv

# Before reading any configuration below: the environment, then a .env in the folder you run from (or
# a folder above it), then ~/.coding-agent/.env -- a setting found first wins. usecwd: without it,
# python-dotenv searches from where this package is installed, not from where you run the agent.
load_dotenv(find_dotenv(usecwd=True))
load_dotenv((Path(os.environ["HOME"]).expanduser() if os.environ.get("HOME") else Path.home()) / ".coding-agent" / ".env")


DEFAULT_MODEL = "claude-sonnet-5"
# DeepSeek's Anthropic-compatible API (DEEPSEEK_API_KEY): same client, another base URL and model.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_EFFORT = "medium"   # how much the model thinks and writes (CODEAGENT_EFFORT)
DEFAULT_THINKING = "off"    # thinking before the answer (CODEAGENT_THINKING)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
THINKING_MODES = ("adaptive", "between_tools", "disabled")

# What a host application sets while it runs (a Settings screen), see configure(): it wins over the
# environment. Kept in this process only, never in os.environ, so the programs the agent starts do not
# inherit the key.
_lock = threading.RLock()
_overrides: dict[str, str] = {}
_clients: dict[tuple, Anthropic] = {}


def configure(api_key: str | None = None, model: str | None = None, effort: str | None = None) -> None:
    """Use this Anthropic API key and / or model from now on, in every later call (None leaves a setting as
    it is, "" removes it). A key given here selects Anthropic's own API even when a Foundry endpoint is set in
    the environment: the person typed it, so it wins. The model applies on Anthropic's API only (on Foundry a
    model is a deployment name). `effort` is the thinking effort (see get_effort(); "default" sends none)."""
    with _lock:
        for name, value in (("api_key", api_key), ("model", model), ("effort", effort)):
            if value is None:
                continue
            value = value.strip()
            if value:
                _overrides[name] = value
            else:
                _overrides.pop(name, None)


def clear() -> None:
    """Forget what configure() set, and the clients built so far."""
    with _lock:
        _overrides.clear()
        _clients.clear()


def _deepseek_key() -> str | None:
    """DEEPSEEK_API_KEY, when DeepSeek is the service in use: CODEAGENT_PROVIDER=deepseek, or no other service is set up
    (no key given to configure(), no Foundry endpoint, no ANTHROPIC_API_KEY: those keep their priority)."""
    key = os.environ.get("DEEPSEEK_API_KEY") or None
    if not key or _overrides.get("api_key"):
        return None
    if (os.environ.get("CODEAGENT_PROVIDER") or "").strip().lower() == "deepseek":
        return key
    if os.environ.get("ANTHROPIC_FOUNDRY_ENDPOINT") or os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return key


def current_api_key() -> str | None:
    """The API key in use: the one given to configure(), else DEEPSEEK_API_KEY (when DeepSeek is in use), else ANTHROPIC_API_KEY."""
    return _overrides.get("api_key") or _deepseek_key() or os.environ.get("ANTHROPIC_API_KEY") or None


def uses_anthropic_api() -> bool:
    """A service reached with an API key through the Anthropic client: Anthropic's own API (a key given to configure(), or
    ANTHROPIC_API_KEY when no Foundry endpoint is set) or DeepSeek's (see uses_deepseek()).

    A Foundry setup from the environment stays on Foundry, even with an ANTHROPIC_API_KEY set for other tools.
    """
    if _overrides.get("api_key") or _deepseek_key():
        return True
    return not os.environ.get("ANTHROPIC_FOUNDRY_ENDPOINT") and bool(os.environ.get("ANTHROPIC_API_KEY"))


def uses_deepseek() -> bool:
    """DeepSeek's Anthropic-compatible API: DEEPSEEK_API_KEY in use, or an ANTHROPIC_BASE_URL on deepseek.com. It does not
    take the web_search tool, cache_control or PDF `document` blocks (see active_tools, the loop and read_pdf)."""
    if _deepseek_key():
        return True
    return uses_anthropic_api() and "deepseek.com" in (os.environ.get("ANTHROPIC_BASE_URL") or "").lower()


def active_provider() -> str | None:
    """"anthropic", "deepseek", "foundry", or None when none is set up."""
    if uses_deepseek():
        return "deepseek"
    if uses_anthropic_api():
        return "anthropic"
    return "foundry" if os.environ.get("ANTHROPIC_FOUNDRY_ENDPOINT") else None


def key_location() -> str:
    """Where the key in use comes from, for messages: "the API key saved in Settings" or the variable."""
    if _overrides.get("api_key"):
        return "the API key saved in Settings"
    return "the API key (DEEPSEEK_API_KEY)" if _deepseek_key() else "the API key (ANTHROPIC_API_KEY)"


def model_setting() -> str:
    return "the model chosen in Settings" if _overrides.get("model") else "ANTHROPIC_MODEL"


def make_anthropic_client(api_key: str, **options) -> Anthropic:
    """A client for this key, outside the cache: to test a key before it is saved."""
    return Anthropic(api_key=api_key, **{"max_retries": 2, **options})


def _client_key() -> tuple:
    if uses_anthropic_api():
        return ("anthropic", current_api_key(), bool(_deepseek_key()))
    return ("foundry", os.environ.get("ANTHROPIC_FOUNDRY_ENDPOINT"), os.environ.get("ANTHROPIC_FOUNDRY_API_KEY"))


def _build_client() -> Anthropic:
    if uses_anthropic_api():
        return make_anthropic_client(current_api_key(), **({"base_url": DEEPSEEK_BASE_URL} if _deepseek_key() else {}))
    api_key = os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
    if api_key:
        return AnthropicFoundry(
            api_key=api_key,
            base_url=os.environ["ANTHROPIC_FOUNDRY_ENDPOINT"],
            max_retries=2,
        )
    from azure.identity import get_bearer_token_provider

    from .signin import SignIn  # the Windows account, `az login`, ... or the Microsoft sign-in page

    scope = os.environ.get("TOKEN_SCOPE", "https://ai.azure.com/.default")
    token_provider = get_bearer_token_provider(SignIn(), scope)
    return AnthropicFoundry(
        azure_ad_token_provider=token_provider,
        base_url=os.environ["ANTHROPIC_FOUNDRY_ENDPOINT"],
        max_retries=2,
    )


def _get_client() -> Anthropic:
    """The Claude client for the current settings: Anthropic's API (a key from configure() or ANTHROPIC_API_KEY,
    no Foundry endpoint unless configure() gave the key), otherwise AnthropicFoundry: API key if
    ANTHROPIC_FOUNDRY_API_KEY is set, otherwise Azure AD. Built once per distinct setting and reused; a new key or
    endpoint gets a new client."""
    with _lock:
        key = _client_key()
        client = _clients.get(key)
        if client is None:
            client = _build_client()
            _clients.clear()
            _clients[key] = client
        return client


_get_client.cache_clear = _clients.clear  # callers that want a fresh client (tests)


# On Foundry this is your *deployment name*; on Anthropic's API, a model ID (configure(), else ANTHROPIC_MODEL).
# Read when needed, never copied at import: configure() can change it while the program runs.
def get_model() -> str:
    if uses_anthropic_api():
        return _overrides.get("model") or os.environ.get("ANTHROPIC_MODEL") or (DEEPSEEK_MODEL if _deepseek_key() else DEFAULT_MODEL)
    return os.environ.get("ANTHROPIC_FOUNDRY_DEPLOYMENT") or DEFAULT_MODEL


class SettingError(ValueError):
    """A thinking or effort setting that cannot be used: the message says what is allowed, and is meant for the person."""


def get_effort() -> str | None:
    """How much the model thinks and writes: configure(effort=...), else CODEAGENT_EFFORT, else DEFAULT_EFFORT. One of
    EFFORT_LEVELS, or None for "default" (nothing is sent: the API's own default applies). Case and spaces are ignored.
    Read when needed, never at import."""
    value = (_overrides.get("effort") or os.environ.get("CODEAGENT_EFFORT") or DEFAULT_EFFORT).strip().lower()
    if value == "default":
        return None
    if value not in EFFORT_LEVELS:
        raise SettingError(f"CODEAGENT_EFFORT must be one of {', '.join(EFFORT_LEVELS)} or default (got {value!r}).")
    return value


def get_thinking() -> str | None:
    """Thinking before the answer: CODEAGENT_THINKING, else DEFAULT_THINKING. One of THINKING_MODES, or None when no
    thinking field must be sent. "off" is the lowest setting the model accepts, decided from get_model(): none for
    claude-fable-* (its thinking cannot be turned off), "between_tools" for claude-sonnet-5-5 (it answers 400 to
    disabled), else "disabled". On Foundry get_model() is a deployment name, so "off" may guess wrong: name
    between_tools or disabled yourself then."""
    value = (os.environ.get("CODEAGENT_THINKING") or DEFAULT_THINKING).strip().lower()
    if value == "off":
        model = get_model().lower()
        if "fable" in model:
            return None
        return "between_tools" if "sonnet-5-5" in model else "disabled"
    if value not in THINKING_MODES:
        raise SettingError(f"CODEAGENT_THINKING must be one of off, {', '.join(THINKING_MODES)} (got {value!r}).")
    return value


def thinking_options() -> dict:
    """The request fields for the agent's calls: {"thinking": {"type": mode}} and {"output_config": {"effort": level}}, each
    left out when it is None. claude-haiku-* has no effort parameter, so none is sent for it."""
    mode, effort = get_thinking(), get_effort()
    if "haiku" in get_model().lower():
        effort = None
    if mode == "between_tools" and effort in ("xhigh", "max"):
        raise SettingError(f"Thinking between_tools does not work with effort {effort} (the API answers 400): "
                           "set CODEAGENT_EFFORT to high or lower, or CODEAGENT_THINKING to adaptive.")
    options: dict = {}
    if mode is not None:
        options["thinking"] = {"type": mode}
    if effort is not None:
        options["output_config"] = {"effort": effort}
    return options


def get_memory_model() -> str:
    return os.environ.get("AGENT_MEMORY_MODEL") or get_model()


def get_compact_model() -> str:
    return os.environ.get("AGENT_COMPACT_MODEL") or get_model()


_OLD_NAMES = {"MODEL": get_model, "MEMORY_MODEL": get_memory_model, "COMPACT_MODEL": get_compact_model}


def __getattr__(name: str):
    """The old constants, for programs that read config.MODEL: read when accessed. (`from coding_agent.config
    import MODEL` still copies the value once: use get_model().)"""
    if name in _OLD_NAMES:
        return _OLD_NAMES[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


MAX_TOKENS = 64000  # safe with streaming (no HTTP timeout risk)
MAX_TOOL_OUTPUT_CHARS = 50_000
RUN_TIMEOUT_SECONDS = 120
GIT = shutil.which("git")  # None when git is not installed
GIT_TIMEOUT_SECONDS = 30
CLONE_TIMEOUT_SECONDS = 600
DOWNLOAD_MAX_BYTES = int(float(os.environ.get("AGENT_DOWNLOAD_MAX_MB") or 50) * 1024 * 1024)
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the API's limit per image
MAX_SCREENSHOT_TILES = 4  # full-page screenshots are cut into viewport-sized images
IMAGE_TOKENS = 1600  # rough context cost of one image, for the context estimate
PDF_PAGE_TOKENS = 2500  # rough context cost of one PDF page (text + page image)
PDF_MAX_VISUAL_PAGES = 20  # pages per read_pdf call in visual mode
EXCEL_MAX_CELLS = 3000  # cells shown per read_excel call
EXCEL_MAX_CHANGES = 1000  # cells changed per edit_excel call
# Who changes, formats and renders workbooks: "xlwings" (Excel itself), "openpyxl" (rewrites the file,
# runs anywhere), or "auto": xlwings when it is installed and Excel can be started, else openpyxl.
EXCEL_BACKEND = (os.environ.get("AGENT_EXCEL_BACKEND") or "auto").strip().lower()
WEB_PAGE_CHARS = 5000  # page text shown per web_open / web_click / web_page; the rest is read with web_page(offset=...)
WEB_MAX_CONTROLS = 100  # links and controls listed per page
EXCEL_CELL_CHARS = 200  # a long cell is cut in a sheet read; reading that one cell shows it whole
EXCEL_VIEW_MAX_CELLS = 2000  # cells drawn by view_excel without Excel (openpyxl backend)
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
UV = shutil.which("uv")  # None when uv is not installed
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}

MAX_LISTING_ENTRIES = 500

# Your home folder: $HOME when it is set (as most shells and tools use it), otherwise the OS user
# folder. On Windows Python itself ignores HOME and uses USERPROFILE, which can point elsewhere.
HOME_DIR = Path(os.environ["HOME"]).expanduser() if os.environ.get("HOME") else Path.home()
AGENT_HOME = HOME_DIR / ".coding-agent"
MEMORY_HOME = Path(os.environ.get("AGENT_MEMORY_DIR") or AGENT_HOME / "memory").expanduser()
BACKUP_HOME = AGENT_HOME / "backups"  # previous versions of workbooks changed by edit_excel
PACKAGE_DIR = Path(__file__).resolve().parent  # the agent/ package: its own source code
BUNDLED_SKILLS = PACKAGE_DIR / "skills"
PERSONAL_SKILLS = Path(os.environ.get("AGENT_SKILLS_DIR") or AGENT_HOME / "skills").expanduser()

# Clickable `path:line` links in the terminal (OSC 8 hyperlinks).
EDITOR = os.environ.get("AGENT_EDITOR", "vscode").lower()
# sys.stdout is None when there is no console (e.g. a windowed app on Windows).
LINKS_ENABLED = EDITOR != "none" and sys.stdout is not None and sys.stdout.isatty()
FILE_REF = re.compile(r"((?:[A-Za-z]:[\\/])?[\w.\-/\\]+\.[A-Za-z0-9]+):(\d+)")

# Anthropic's web search tool version: 20250305 works everywhere on Foundry, 20260209 only on
# Anthropic-hosted deployments. "off" removes the tool (e.g. if your organization disabled it).
WEB_SEARCH = (os.environ.get("AGENT_WEB_SEARCH") or "20250305").strip().lower()
if WEB_SEARCH not in ("20250305", "20260209", "off"):
    raise SystemExit(f"AGENT_WEB_SEARCH must be 20250305, 20260209 or off (got {WEB_SEARCH!r})")
# "off": the browsing tools open any public site without asking first (for unattended runs). A form that
# sends data, and passwords or payment fields, still ask; local and metadata addresses stay blocked.
WEB_APPROVE = (os.environ.get("AGENT_WEB_APPROVE") or "on").strip().lower() not in ("off", "0", "false", "no")
WEB_SEARCH_MAX_USES = 5  # searches allowed per model response

MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "100"))  # model calls per instruction

# The agent must never modify its own source code or its bundled skills (the package folder holds
# both); a project's .agent/skills folder is added per workspace (see state.protected_paths).
OWN_FILES = [PACKAGE_DIR, PACKAGE_DIR.parent / "agent.py"]  # the package and its launcher

# Memory is updated in the background by a separate model call after each instruction.
MEMORY_UPDATES = (os.environ.get("AGENT_MEMORY") or "on").strip().lower() not in ("off", "0", "false", "no")
MEMORY_MAX_CHARS = 12_000  # the curator keeps notes.md under this size
MEMORY_EXIT_WAIT_SECONDS = 60

# Context window management; see the "Context management" section below.
DEFAULT_CONTEXT_WINDOW = int(os.environ.get("AGENT_CONTEXT_WINDOW") or 200_000)  # tokens, per deployment
CLEAR_AT = 0.50    # above this share of the window, old tool outputs are cleared
COMPACT_AT = 0.70  # above this share, the earlier conversation is replaced by a summary
KEEP_RECENT_RESULTS = 4  # tool-result messages that are never cleared (the latest ones)
CHARS_PER_TOKEN = 3.5  # rough, for estimating what was added since the last API call
CLEARED_NOTE = "[output cleared to save context -- call the tool again if you need it]"

