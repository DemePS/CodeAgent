# CodeAgent

An agent on Claude, running in your Azure subscription (Microsoft Foundry): it reads and edits files,
Excel workbooks and PDFs in a project folder, asks before every change, and remembers what it learned
about each project. Use it from the terminal, or as a library to build your own tool on top of it
(for example a desktop app that fills Excel workbooks from PDF documents).

```bash
pip install codeagent
```

Not on PyPI yet (see [Publishing to PyPI](#publishing-to-pypi-to-do)). Until then, from a clone of
this repository, run `uv sync`: it installs the package and the `coding-agent` command in `.venv`
(`uv run coding-agent`). An application built on it just runs `uv sync` too: its own `pyproject.toml`
and `uv.lock` say which version of the engine to fetch.

The package installs the `coding_agent` Python package and the `coding-agent` command.
Python 3.10 or later; Windows, macOS and Linux.

## Configure

Environment variables, or a `.env` file in the folder you run from (or `~/.coding-agent/.env`):

| Variable | Meaning |
|---|---|
| `ANTHROPIC_FOUNDRY_ENDPOINT` | `https://<resource>.services.ai.azure.com/anthropic` |
| `ANTHROPIC_FOUNDRY_DEPLOYMENT` | your Claude deployment name |
| `ANTHROPIC_FOUNDRY_API_KEY` | an API key; leave unset to sign in with your Microsoft work account |

Without an API key, the agent signs in with the account of your Windows session, a developer's
`az login`, or else the Microsoft sign-in page (once; the account is remembered).

**Without Azure**, use Anthropic's own API: leave `ANTHROPIC_FOUNDRY_ENDPOINT` unset and set

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | an API key from [console.anthropic.com](https://console.anthropic.com) |
| `ANTHROPIC_MODEL` | a model ID (default `claude-opus-5`) |

A Foundry endpoint always wins: with `ANTHROPIC_FOUNDRY_ENDPOINT` set, the agent uses Foundry even if
`ANTHROPIC_API_KEY` is also set.

## Use it in the terminal

```bash
coding-agent -d path/to/project "Add input validation to the CLI"
coding-agent -d path/to/project            # interactive
coding-agent -d path/to/project -r         # resume the last conversation
coding-agent -d path/to/project --read D:/docs --read //server/share   # also read (never write) other folders
coding-agent --check                      # Claude not answering? test each step of a call
coding-agent --help
```

## Use it as a library

```python
from coding_agent import session
from coding_agent.ui import TerminalUI  # or your own UI (subclass coding_agent.ui.UI)

session.open_project("path/to/folder", ui=TerminalUI(),
                     tools=["read_excel", "edit_excel", "read_pdf", "ask_human"],  # optional subset
                     system_prompt=None)                                           # or your own instructions
ok, message = session.check_connection()   # can Claude be reached?
session.send("Fill costs.xlsx from invoice.pdf")
session.close()
```

`session.stop()` stops the running instruction from another thread (e.g. a Stop button);
`session.add_read_folder(path)` lets the agent read (never write) another folder. Every approval,
question and progress message goes through the UI object, so a web or desktop front end can show them.

## Tools

Files (`read_file`, `write_file`, `edit_file`, `list_directory`, `grep`, `copy_path`, `delete_file`,
`delete_folder`, `change_directory`), documents (`read_pdf`, `view_image`, `read_excel`,
`edit_excel`, `view_excel`, `format_excel`, `add_chart`, `add_table`, `add_pivot_table`, `restore_backup`), `git` (read-only), `run_python` (sandboxed),
`download_file`, `clone_repo`, `web_search`, `screenshot_page` (`uv sync --extra browser`), `ask_human`,
`load_skill`.

### Excel: two backends

Who changes, formats and renders workbooks is set by `AGENT_EXCEL_BACKEND`:

| | `openpyxl` | `xlwings` (`uv sync --extra excel`) |
|---|---|---|
| Runs on | anywhere | Windows or macOS with Excel installed |
| Saving | rewrites the file: charts, pictures, pivot tables can be damaged (you are warned first) | Excel saves: nothing is lost |
| Formulas after an edit | old results until the file is opened in Excel | recalculated at once |
| `view_excel` | a drawing of the cells (needs the `browser` extra); charts listed, not drawn | what Excel prints, charts included |
| A workbook open in Excel | refused (locked) | written in that window, if it has no unsaved changes |
| `add_chart`, `add_table` | written by openpyxl | added by Excel |
| `add_pivot_table` | refused (openpyxl cannot build them) | built by Excel |

`auto` (the default) uses xlwings when it is installed and Excel can be started, else openpyxl.
Reading (`read_excel`) is the same in both: it never changes the file, lists the charts, pictures,
tables and pivot tables of a sheet, and shows a long cell whole when that one cell is read.
`scripts/compare_excel_backends.py` runs the same edits, formatting and views with both backends and
reports what each kept (run it on a PC with Excel: `uv run --extra excel --extra browser python
scripts/compare_excel_backends.py [your.xlsx ...]`).

### Excel scripts (`run_python_excel`, off by default)

For what no Excel tool does, `AGENT_EXCEL_SCRIPTS=on` gives the agent `run_python_excel`: a Python
script run with Excel (xlwings backend only) on a workbook, which it gets as `book`. Excel can do far
more than the `run_python` sandbox can see (run macros, start programs, open and save any file), so:

- the script is checked before it runs, and refused if it uses macros (`Run`, `Evaluate`,
  `VBProject`, `ExecuteExcel4Macro`...), programs or links (`Shell`, `FollowHyperlink`, DDE
  formulas such as `=cmd|...`), other files or workbooks (`save`, `SaveAs`, `Open`, `books`,
  `app`, `Application`, `Parent`), external data (`QueryTables`, `WEBSERVICE`), imports beyond
  `math`, `datetime`, `re`, `collections`... , or ways around the check (`eval`, `exec`, `getattr`
  with a computed name, `_private` attributes);
- you approve the script (unless autonomous mode is on), and a backup of the workbook is kept first
  (`restore_backup` undoes it);
- it runs in its own invisible Excel with macros forced off and events off, inside the `run_python`
  sandbox; macros it adds are refused and the workbook is put back;
- you see what it changed: cells, sheets, charts, tables, pivot tables.

The check reads the script; it cannot prove what Excel will do. Turn it on for developers on their
own machines, never in an application for end users.

## Safety

- It writes only inside the project folder; extra folders you add are read-only.
- Every file or workbook change is shown (a diff, or a cell-by-cell table) and waits for your
  approval; a copy of the previous version of each workbook is kept in `~/.coding-agent/backups/` for 3 days,
  and `restore_backup` puts one back ("undo your changes to costs.xlsx"). Applications can list and
  restore them with `coding_agent.backups` (`versions()`, `restore()`).
- Deleting always asks, even in autonomous mode; downloads and clones always ask; the agent cannot
  modify its own files; `run_python` cannot start processes or delete files.

## What stays on your machine

Everything is in `~/.coding-agent` (`$HOME`, or your user folder on Windows), cleaned at each start
of `coding-agent` (`coding_agent/cleanup.py`; applications call `coding_agent.cleanup.run()`):

| What | Where | Kept |
|---|---|---|
| Copies of workbooks before each change | `backups/` | 3 days (`AGENT_BACKUP_DAYS`) |
| What the agent learned about each project | `memory/<project>-<code>/` | notes capped in size; deleted after 90 days unused (`AGENT_MEMORY_DAYS`) |
| The saved conversation (`--resume`) | `memory/<project>-<code>/conversation.json` | 30 days (`AGENT_CONVERSATION_DAYS`) |

`AGENT_MEMORY=off` stops the agent from writing memory at all.

For developers, to see and delete what the agent remembers:

```bash
coding-agent --memories              # every project's memory: name, last used, size, notes
coding-agent --memories myapp        # one project's notes, in full
coding-agent --forget myapp          # delete one project's memory (asks first)
coding-agent --forget . -d path      # the memory of that project folder
coding-agent --forget all            # delete every project's memory (asks first)
```

## Development

```bash
uv sync            # the exact versions the tests ran with (uv.lock)
uv run pytest
uv build           # dist/codeagent-<version>.tar.gz and .whl
```

## Publishing to PyPI (to do)

The workflow (`.github/workflows/package.yml`) is ready but nothing has been published yet: the
applications install the engine from a GitHub commit meanwhile. To publish a version:

1. **Once, on pypi.org** -- Your account > Publishing > *Add a new pending publisher*: PyPI project
   `codeagent`, owner `DemePS`, repository `CodeAgent`, workflow `package.yml`, environment `pypi`.
   (The api-management branch publishes `codeagent-apim` the same way: add a second pending publisher
   with that project name.)
2. **For each release**, tag the commit whose `pyproject.toml` has that version, and push the tag:
   ```bash
   git fetch origin
   git tag -a v0.5.0 origin/main -m "v0.5.0"     # api-management: apim-v0.5.0 on origin/api-management
   git push origin v0.5.0
   ```
   The tag runs the tests and the build, publishes to PyPI (trusted publishing: no token stored) and
   creates a GitHub Release. A tag whose version differs from `pyproject.toml` is refused. If the tag
   was pushed before step 1, only the publish job fails: re-run it (Actions > the run > *Re-run
   failed jobs*) once PyPI is set up.
3. **Afterwards**, the applications can depend on the published package (`codeagent==0.5.0`) and drop
   their `[tool.uv.sources]` git pin (then `uv lock`).

## License

MIT (see LICENSE).
