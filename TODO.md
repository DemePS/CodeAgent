# TODO

## DeepSeek (when the key is available)
- Set `DEEPSEEK_API_KEY` and run `coding-agent --check`, then one instruction that uses tools.
- Still unverified with a real call: the context window of `deepseek-flash` (set `AGENT_CONTEXT_WINDOW`), the
  `thinking: disabled` field, and the wording of DeepSeek's "prompt too long" error (`learn_window` parses Anthropic's).
- Visual `read_pdf` is refused on DeepSeek: pages could be sent as images instead (needs a PDF renderer).

## Tests
- `tests/test_loop.py::test_every_tool_call_and_its_outcome_reach_the_ui` fails in the full suite but passes alone:
  another test leaves state behind (approvals or auto mode). Find it.
- `tests/test_browser.py::test_a_page_that_reloads_itself_still_gives_a_screenshot` failed once in a full run (flaky).

## Caching
- With `AGENT_CACHE_TTL=1h`, the web search tool inserts its own 5-minute cache points after tool results; check that a
  1-hour request with web search is accepted (longer TTLs must come before shorter ones).

## Docs
- `example.env` still calls `claude-haiku-4-5` the cheapest model: `claude-haiku-5-5` is ($0.10 / $0.50 per million
  tokens up to 100K-token prompts) and a better choice for `AGENT_MEMORY_MODEL` and `AGENT_COMPACT_MODEL`. Check that
  "off" thinking and the effort settings suit it.

## New project: the seedagent tool set on Microsoft Agent Framework (not started)
Goal: a **separate** project (new top-level folder, e.g. `AgentFrameworkAgent/`; seedagent itself is not
modified) that runs Claude on Azure/Microsoft Foundry behind a **Microsoft Agent Framework** (MAF) agent,
with the seedagent tools.

What MAF gives (verified in the docs, 09/01/2026 — links below):
- **`@tool` accepts a raw JSON schema dict** (`schema=`), a Pydantic model, or type annotations, so the 45
  schemas in `seedagent/schemas.py` transfer unchanged; handlers are already plain functions
  (`seedagent/tools/__init__.py:55-102`).
- **Claude on Foundry is a first-class provider**: `pip install agent-framework-anthropic --pre`, then
  `AnthropicClient(model=..., api_key=..., base_url="https://<resource>.services.ai.azure.com/models/anthropic")`,
  or the Foundry variants (API key / `DefaultAzureCredential`). Function tools are supported on the
  Anthropic provider (per the tool support matrix).
- **Tool approval**: per-tool approval gate; the run returns `ToolApprovalRequestContent` (tool name +
  arguments) instead of running it, and the caller answers and feeds the decision back. Only works for
  locally-invoked tools, which is what these are.
- **`ClaudeAgent`** (Agent Framework's wrapper of the Claude Agent SDK) is the other column: Claude's
  runtime owns sessions, permissions and built-in tools. We want the **direct model inference** column
  ("your application owns the loop, sessions, middleware, function tools").
- **UI**: DevUI (`pip install agent-framework-devui --pre`, `serve(entities=[agent], auto_open=True)`,
  http://localhost:8080, OpenAI-compatible `/v1`) is a **sample/dev** app, explicitly not for production.
  AG-UI = protocol + client renderer for a custom frontend. ChatKit = OpenAI chat components.
  **None of them is a terminal agent with per-tool approval prompts and a Stop button** — that stays ours.

What does not transfer (the real work):
- Handlers read `state.*`: `workspace`, `cwd`, `ui`, `tool_names`, `auto_mode` (`seedagent/state.py:260-281`).
  MAF needs a runtime/context object passed in, so every handler signature gains a context parameter.
- Per-session approval policy and "always allow python / az / uv" memory (`state.py:109-111`) — ours. Same
  for autonomous mode, protected paths, read roots, the Excel sheet/column restrictions (`state.py:84-98`),
  web-site approvals.
- The `UI` stream contract (`loop.py:147-180`): bridge MAF's update stream to the same hooks; approvals map
  from `ToolApprovalRequestContent`; Stop button stays a thread watching a flag.
- `compact` / `clear_old_tool_results` / `learn_window` / overflow retry (`loop.py:183-197`) — ours.
- `ToolError` → model-visible error (`tools/__init__.py:170-183`) — map to MAF's error result.
- Handlers are sync, MAF is async: wrap with `asyncio.to_thread`, but check the browser thread and the
  xlwings/Excel COM threading assumptions first.
- `run_python`'s sandbox and the Excel backend are ours: MAF's Code Interpreter is a different, sandboxed
  environment and would not reproduce them.

Open decisions (answer before writing code):
1. **A or B** — (A) port the tool modules into the new package, `state.x` → `ctx.x`, no dependency on
   seedagent but two copies that will drift; (B) register the existing handlers
   (`seedagent.tools.TOOL_HANDLERS[name]`) as MAF tools and make a seedagent `Session` current around each
   run — one copy of every tool, but the new app's workspace/approvals live in seedagent's session object.
   Recommendation: **B first**, port module by module later.
2. **Foundry resource, Claude model, auth** — e.g. `zh-aif-maia-core-bo-d-swe-1` + a Claude deployment
   name; API key or `DefaultAzureCredential`.
3. **Scope** — all 45 tools (incl. `mail_*`, `send_sms`, the web browser) or only the local/document ones
   that need no credentials?
4. Folder name.

Suggested layout:
```
AgentFrameworkAgent/
├── pyproject.toml      # agent-framework, agent-framework-anthropic, azure-identity, (agent-framework-devui)
├── .env.example        # resource, model, auth mode — no secrets
├── src/<pkg>/
│   ├── settings.py     # base_url / model / credential, read lazily at call time
│   ├── context.py      # what every tool needs: workspace, cwd, approvals, ui
│   ├── approvals.py    # which tools ask; "always allow"; autonomous mode
│   ├── ui.py           # terminal output + stream bridge
│   ├── tools/          # the ported tools (schemas reused)
│   ├── agent.py        # Agent(client=AnthropicClient(base_url=...), tools=[...])
│   ├── console.py      # the approval-prompting terminal front end
│   └── serve.py        # DevUI, for developing/watching a run
├── agent.py            # entry point
└── tests/              # per tool: schema validity, missing credential, path escape
```

Order of work: (1) one tool end to end (`read_file`) against Foundry; (2) the console front end with one
approval-required tool (`delete_file`); (3) the rest of the tools in groups (files, documents, excel,
network/browser, mail/sms); (4) `serve.py` with DevUI; (5) tests per tool.

Sources:
- https://learn.microsoft.com/en-us/agent-framework/agents/tools/ (tool types + support matrix)
- https://learn.microsoft.com/en-us/agent-framework/agents/tools/function-tools?pivots=programming-language-python
- https://learn.microsoft.com/en-us/agent-framework/agents/tools/tool-approval
- https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/model-providers/anthropic?pivots=programming-language-python
- https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/ui/devui/?pivots=programming-language-python

## Untracked files
- `candidatures/`, `etablissements/`, `logement/`, `scripts/test_coding_agent.py` (and `tomodify`,
  `shortlist_appartements_strasbourg.md`): not part of the agent; move them out of the repo or ignore them.
