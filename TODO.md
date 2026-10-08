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

## Untracked files
- `candidatures/`, `etablissements/`, `logement/`, `scripts/test_coding_agent.py` (and `tomodify`,
  `shortlist_appartements_strasbourg.md`): not part of the agent; move them out of the repo or ignore them.
