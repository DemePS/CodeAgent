# CodeAgent (codeagent-apim)

> **This is `codeagent-apim`**: the [`codeagent`](https://pypi.org/project/codeagent/) engine plus
> sign-in for an organization's **Azure API Management gateway** in front of Claude:
> - `ANTHROPIC_FOUNDRY_CLIENT_ID`: the organization's app registration (a public client). Tokens come
>   from the account signed into Windows (silently), or else the Microsoft sign-in page, once; the
>   account is remembered per app registration. `TOKEN_SCOPE` = `api://<gateway API app id>/.default`,
>   `ANTHROPIC_FOUNDRY_ENDPOINT` = the gateway's URL.
> - `coding_agent.signin.access_token(scope)`: one shared sign-in for the application and Claude's
>   calls (e.g. an access check at startup), so there is never more than one sign-in page.
> - `coding_agent.config.CLIENT_HEADERS`: headers sent with every request (e.g. app name and version,
>   for the gateway's logs).
>
> Install it instead of `codeagent` (both provide the `coding_agent` package):
> `pip install codeagent-apim`. Without these settings it behaves like `codeagent`.

An agent on Claude, running in your Azure subscription (Microsoft Foundry): it reads and edits files,
Excel workbooks and PDFs in a project folder, asks before every change, and remembers what it learned
about each project. Use it from the terminal, or as a library to build your own tool on top of it
(for example a desktop app that fills Excel workbooks from PDF documents).

```bash
pip install codeagent-apim    # or codeagent, without the gateway sign-in
```

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

## Use it in the terminal

```bash
coding-agent -d path/to/project "Add input validation to the CLI"
coding-agent -d path/to/project            # interactive
coding-agent -d path/to/project -r         # resume the last conversation
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
`edit_excel`), `git` (read-only), `run_python` (sandboxed), `download_file`, `clone_repo`,
`web_search`, `screenshot_page` (`pip install "codeagent-apim[browser]"`), `ask_human`, `load_skill`.

## Safety

- It writes only inside the project folder; extra folders you add are read-only.
- Every file or workbook change is shown (a diff, or a cell-by-cell table) and waits for your
  approval; a copy of the previous version of each workbook is kept in `~/.coding-agent/backups/` for 3 days.
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

## Development

```bash
uv sync            # the exact versions the tests ran with (uv.lock)
uv run pytest
uv build           # dist/codeagent-<version>.tar.gz and .whl
```

## License

MIT (see LICENSE).
