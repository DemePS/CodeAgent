"""Personal coding agent: Claude on Azure (Microsoft Foundry) with a manual tool-use loop.

Tools:
  - list_directory   : list the folders and files in a directory
  - change_directory : move the agent's current directory (never outside the workspace)
  - grep        : regex search across files in the workspace
  - read_file   : read a file (optionally a line range)
  - edit_file   : replace an exact snippet in a file -- shows a diff and asks permission first
  - write_file  : create/overwrite a file -- shows a diff and asks permission first
  - download_file : download a URL (http/https) into the workspace -- always asks, even in
                  autonomous mode; size-capped (AGENT_DOWNLOAD_MAX_MB, default 50); cloud metadata
                  and link-local addresses are refused (they can leak Azure managed-identity tokens)
  - clone_repo  : git clone an https or ssh repository into a new folder of the workspace -- always
                  asks, even in autonomous mode; shallow by default; hooks and non-network
                  protocols (file://, ext::) are disabled
  - screenshot_page : open a page in headless Chromium (your dev server, a local HTML file, or a
                  public site) and show Claude the screenshot, plus console errors and failed
                  requests -- Claude sees the image itself (text, layout, colors), no OCR needed.
                  Needs Playwright: `pip install playwright` (or `uv add --dev playwright`), then
                  `playwright install chromium` -- or it uses the installed Edge / Chrome when that
                  download is blocked. `coding-agent --check-browser` tests it. localhost / private addresses and workspace files
                  open without asking; public sites always ask. AGENT_BROWSER_PATH picks a specific
                  Chromium/Chrome/Edge executable.
  - view_image  : show Claude an image from the workspace (a mockup, a design export, a screenshot)
  - read_pdf    : give Claude a PDF's pages as a document it reads itself -- text, tables, layout and
                  scanned pages -- or just the extracted text for long documents
  - read_excel  : list a workbook's sheets and show cells (values and formulas) of a sheet or range
  - edit_excel  : set cell values or formulas in an .xlsx/.xlsm (or create a new workbook) -- shows a
                  cell-by-cell diff and asks first; a copy of the previous file is kept in
                  $HOME/.coding-agent/backups/; warns (and always asks) when the workbook has charts,
                  images or pivot tables, which openpyxl cannot keep (with Excel doing the saving --
                  AGENT_EXCEL_BACKEND=xlwings, the default when Excel and xlwings are there -- nothing is lost)
  - view_excel  : see a sheet or range as the person sees it (Excel's rendering with charts, or a drawing)
  - format_excel: bold, fills, borders, wrap, widths, number formats, frozen header, filters
  - add_chart, add_table, add_pivot_table : add a chart, an Excel table or a pivot table (pivot
                  tables need Excel) -- shows what will be added and asks first; a backup is kept
  - copy_path   : copy a file or a folder inside the workspace -- a text file shows a diff, a
                  binary file or folder shows what will be created; asks permission first
  - delete_file : delete a file -- always asks for human validation, even in autonomous mode
  - delete_folder : delete a folder and everything in it -- shows what it contains and always
                  asks for human validation, even in autonomous mode; never the workspace root,
                  a .git folder, or a folder holding the agent's own files
                  (edit_file, write_file, delete_file and delete_folder never touch the agent's own source)
  - ask_human   : lets the model ask you a question mid-task
  - git         : read-only git -- status, diff and log of the workspace (never commits, checks
                  out or changes anything; external diff tools, textconv filters, pagers and
                  fsmonitor hooks are disabled so a repository's config cannot run commands)
  - run_python  : run a Python snippet, script or module (e.g. pytest) -- asks permission first
                  (uses `uv run --frozen/--no-sync` when uv is installed: the project's own
                  environment, never rewriting uv.lock; the code cannot start, replace or kill
                  processes or modify files, including through ctypes -- see GUARD_SOURCE)
  - load_skill  : load a skill's full instructions when a task matches it
  - web_search  : Anthropic's server-side web search (runs on Anthropic's side; nothing executes
                  locally). AGENT_WEB_SEARCH=20250305 (default; the only version on Foundry
                  deployments hosted on Azure), 20260209 (better filtering; Anthropic-hosted
                  deployments), or off.

Skills are folders with a SKILL.md (a `name` / `description` header, then instructions), found in:
    coding_agent/skills/                 -- shipped with the agent
    $HOME/.coding-agent/skills/          -- personal, every project (override with AGENT_SKILLS_DIR)
    <project>/.agent/skills/             -- per project (can be committed)
A later location overrides an earlier one with the same skill name. Only names and descriptions
are sent up front; Claude loads a skill's instructions when it needs them.

Memory: notes about each project, kept between runs in
$HOME/.coding-agent/memory/<project>/memories/notes.md (override the root with AGENT_MEMORY_DIR).
They are given to Claude at the start of each session. Updating them never slows the agent down:
after each instruction a background thread sends a summary of what happened to a separate
"memory curator" call, which rewrites notes.md only when something durable was learned. You get
the next prompt right away; "[memory] ..." shows when the update finishes. AGENT_MEMORY_MODEL
picks the deployment it uses (default: the main one -- a smaller, cheaper one works well);
AGENT_MEMORY=off disables updates. On exit the agent waits (up to 60 s) for a pending update.

Configuration (environment variables or a .env file):
    ANTHROPIC_FOUNDRY_ENDPOINT     https://<resource>.services.ai.azure.com/anthropic
    ANTHROPIC_FOUNDRY_API_KEY      API key; leave unset to sign in with a Microsoft work account: the
                                   Windows session's account, `az login`, ... or else the Microsoft
                                   sign-in page, once (see coding_agent/signin.py)
    ANTHROPIC_FOUNDRY_DEPLOYMENT   your Claude deployment name
    AZURE_TENANT_ID / AZURE_CLIENT_ID  tenant and app registration for the sign-in page (optional)
    ANTHROPIC_FOUNDRY_BROWSER_SIGN_IN=0  never open the sign-in page (servers, CI)

    Or, without Azure: Anthropic's own API (used only when ANTHROPIC_FOUNDRY_ENDPOINT is not set)
    ANTHROPIC_API_KEY              an API key from console.anthropic.com
    ANTHROPIC_MODEL                a model ID (default claude-opus-5)

Usage:
    uv sync                                   # once, in the agent's folder (add --extra browser for screenshots)
    uv run coding-agent -d path/to/project "Add input validation to the CLI"
    python agent.py -d path/to/project "..."      # same thing, without installing
    coding-agent -d path/to/project "..." -i      # keep chatting after the task
    coding-agent -d path/to/project               # interactive mode only
    coding-agent -d path/to/project -r            # resume the last conversation in this project
    coding-agent -d path/to/project "..." --auto  # autonomous mode (see below)
    coding-agent -d path/to/project --where       # show where memory and skills are read from

As a library (other front ends, e.g. a desktop app), see coding_agent.session:
    from coding_agent import session
    session.open_project(path, ui=MyUI(), tools=[...], system_prompt="...")
    session.send("instruction")
The package layout: config (settings), state (the session), ui (the UI interface and the
terminal), loop (the agent loop), tools/ (one module per tool family), guard (the run_python
sandbox), skills, memory, context (context window management), cli (the terminal front end).
    (in interactive mode, /skills re-scans the skill folders and shows them)

$HOME is used when set; otherwise your user folder (on Windows, %USERPROFILE%).

Autonomous mode (--auto, or /auto in interactive mode to toggle, /mode to show): edits,
new files and run_python are applied without asking (diffs are still printed), and ask_human
does not wait -- Claude decides and states its assumptions. Deleting a file or a folder always
waits for your approval, even in autonomous mode. Workspace confinement,
self-protection and the subprocess block still apply; Ctrl+C stops it. AGENT_MAX_STEPS (default
100) caps the model calls per instruction in every mode.

Context management (long sessions): the agent tracks how much of the model's context window
the conversation uses and prints it after each instruction ("[context] 84k / 200k tokens").
Past 50% it replaces old tool outputs with a short note (Claude re-reads files when needed);
past 70% it compacts: a summary call replaces the earlier conversation with a brief (goal,
decisions, files changed, state, next steps). If the API still says the prompt is too long, it
compacts and retries once. AGENT_CONTEXT_WINDOW (default 200000) is your deployment's window;
AGENT_COMPACT_MODEL picks the deployment that writes summaries (default: the main one).
Pasting: multi-line text pasted at the "You:" prompt (a traceback, a code snippet) is sent as
one instruction. You can also type three double quotes on a line of their own, then paste or
type anything, and end with three double quotes on their own line again. Approval prompts ignore anything typed or pasted before they appear, so
leftover pasted lines can never answer "Apply this change?".

Interactive commands: /context shows usage, /compact compacts now, /clear starts a fresh
conversation (memory notes are kept).

`path:line` references in the output are clickable links that open the file at that line.
Set AGENT_EDITOR to vscode (default), cursor, file, or none.
"""

from .certificates import use_system_certificates as _use_system_certificates

_use_system_certificates()  # HTTPS behind a company proxy: trust what the operating system trusts

