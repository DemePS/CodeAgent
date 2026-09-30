"""Configuration: environment variables (or a .env file), limits, paths and the Claude client."""

import os
import re
import shutil
import sys
from functools import lru_cache
from pathlib import Path

from anthropic import Anthropic, AnthropicFoundry
from dotenv import load_dotenv

load_dotenv()  # before reading any configuration below


def uses_anthropic_api() -> bool:
    """Anthropic's own API: only when no Foundry endpoint is set and ANTHROPIC_API_KEY is.

    A Foundry setup always stays on Foundry, even with an ANTHROPIC_API_KEY set for other tools.
    """
    return not os.environ.get("ANTHROPIC_FOUNDRY_ENDPOINT") and bool(os.environ.get("ANTHROPIC_API_KEY"))


@lru_cache(maxsize=1)
def _get_client() -> Anthropic:
    """Anthropic client (ANTHROPIC_API_KEY, no Foundry endpoint), otherwise AnthropicFoundry:
    API key if ANTHROPIC_FOUNDRY_API_KEY is set, otherwise Azure AD."""
    if uses_anthropic_api():
        return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=2)
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


# On Foundry this is your *deployment name*; on Anthropic's API, a model ID (ANTHROPIC_MODEL).
MODEL = (os.environ.get("ANTHROPIC_MODEL") if uses_anthropic_api() else os.environ.get("ANTHROPIC_FOUNDRY_DEPLOYMENT")) or "claude-opus-5"
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
WEB_SEARCH_MAX_USES = 5  # searches allowed per model response

MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "100"))  # model calls per instruction

# The agent must never modify its own source code or its bundled skills (the package folder holds
# both); a project's .agent/skills folder is added per workspace (see state.protected_paths).
OWN_FILES = [PACKAGE_DIR, PACKAGE_DIR.parent / "agent.py"]  # the package and its launcher

# Memory is updated in the background by a separate model call after each instruction.
MEMORY_UPDATES = (os.environ.get("AGENT_MEMORY") or "on").strip().lower() not in ("off", "0", "false", "no")
MEMORY_MODEL = os.environ.get("AGENT_MEMORY_MODEL") or MODEL
MEMORY_MAX_CHARS = 12_000  # the curator keeps notes.md under this size
MEMORY_EXIT_WAIT_SECONDS = 60

# Context window management; see the "Context management" section below.
DEFAULT_CONTEXT_WINDOW = int(os.environ.get("AGENT_CONTEXT_WINDOW") or 200_000)  # tokens, per deployment
CLEAR_AT = 0.50    # above this share of the window, old tool outputs are cleared
COMPACT_AT = 0.70  # above this share, the earlier conversation is replaced by a summary
KEEP_RECENT_RESULTS = 4  # tool-result messages that are never cleared (the latest ones)
COMPACT_MODEL = os.environ.get("AGENT_COMPACT_MODEL") or MODEL
CHARS_PER_TOKEN = 3.5  # rough, for estimating what was added since the last API call
CLEARED_NOTE = "[output cleared to save context -- call the tool again if you need it]"

