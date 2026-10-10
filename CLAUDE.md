# CLAUDE.md

Notes for working on this repository (CodeAgent: the engine under WaxalAgent, IslamChat, SenAssurChat and AppartFinder).

## Running things

- Tests: `uv run python -m pytest tests -q`. Three tests already fail without any change and are not caused by new work:
  `tests/test_loop.py::test_session_with_tool_subset_and_custom_prompt`,
  `tests/test_mail.py::test_web_sign_in_refuses_mailboxes_and_points_to_mail_draft`,
  `tests/test_provider.py::test_model_setting_follows_the_service`.
- The library index (search_library): `uv run coding-agent --index FOLDER` (OCR of scans included, slow, once);
  `AGENT_INDEX_DIR` and `AGENT_OCR_DIR` move the index and the OCR cache (use a temp folder in experiments).
- Text of PDFs is read by pdfium, not pypdf (pypdf plus fontTools garbled most pages of one PDF).

## Later: run the read-only tool calls of one response concurrently (branch `concurrency`)

Status: not started. The branch `concurrency` exists, at the same commit as `main`, for the experiment.

Why: a model response often asks for several tools at once (2 to 4 `search_library`, `read_file`, `read_pdf`). `coding_agent/loop.py`
runs them one after another: `results = [run_tool(b) for b in tool_uses]`. The searches are milliseconds, but a read of 5 pages or a
few greps add up, and the model is waiting. `search_library` already searches its `a | b | c` alternatives in a thread pool
(`index.MAX_PARALLEL`); asyncio is not the tool for this (nothing waits on a network, it is SQLite and Python work), threads are.

What I checked in the code (can they have separate states?):
- `search_library`, `search_pdf`, `read_pdf`, `read_file`, `grep`, `list_directory` do not write the shared `state` module themselves.
- They all resolve paths with `common.resolve_readable`, which **reads `state.cwd`** (changed by `change_directory`, and by
  `delete_folder` when it removes the current folder) and, in the terminal agent only (`state.ask_read_outside`), can call
  `state.ui.confirm(...)` and **write** `state.read_roots` / `state.read_denied`. Two concurrent prompts would interleave.
- `read_pdf` reads `state.turn["excel_read"]` (a rule that makes the agent open the spreadsheet first); read it, never write it.
- `usage.record_tool`, `state.ui.status`, the tool-result log lines are shared and must stay thread-safe (usage has a lock).
- NOT safe to run together: any tool that changes files, `change_directory`, the `web_*` tools (one browser session), `ask_human`,
  anything that needs an approval.

Plan:
1. A small allow-list of read-only tools (the six above). A batch runs concurrently only if every call in it is on the list and
   `change_directory` / `delete_*` are not in the batch; otherwise unchanged (sequential).
2. Snapshot `state.cwd` once for the batch, so a call cannot see a directory that another call of the same batch changed.
3. Keep the order of the results exactly as the order of the tool calls (the API requires one result per tool_use id).
4. If a call would prompt (`ask_read_outside` and a new folder), run that batch sequentially.
5. Exceptions of one call stay in its own result (an `is_error` tool result), as today.
6. Measure before/after on a batch of 4 reads; add tests: order of results, one failing call, a batch with `change_directory`.
