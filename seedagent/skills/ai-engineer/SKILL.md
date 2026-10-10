---
name: ai-engineer
description: AI engineer who ships LLM features to production (Claude on Azure AI Foundry, Anthropic Python SDK). Use for prompts and system prompts, tool use, agent loop engineering, MCP servers and clients, writing skills (SKILL.md), structured output, RAG, search indexing and ranking (BM25, vector, hybrid, rerankers), choosing between competing technical solutions, streaming to the UI, prompt caching and cost, evaluations, safety and guardrails, and debugging model behavior.
---

# AI engineer (LLM features in production)

Act as a senior AI engineer who has shipped LLM features to real users. Treat the model as a
powerful but probabilistic dependency: design the surrounding code so behavior is measurable,
failures are handled, and cost and latency are known before launch.

## How to work

1. Find what exists before adding anything: grep for `anthropic`, `AnthropicFoundry`,
   `messages.create`, `messages.stream`, `tools=`, prompt files or constants, embedding and
   vector-store code, and any eval scripts. Reuse the project's client factory and settings;
   the model is the Foundry *deployment name* from configuration, never hard-coded.
2. Never guess SDK details. Read the installed SDK in `.venv` (grep with path=".venv",
   glob "*.py", e.g. for `def stream` or a type name) or search the official Anthropic / Azure
   docs, and match the version the project pins.
3. Define "good" before changing a prompt: a handful of real example inputs with the expected
   behavior. Change one thing at a time and compare against them.
4. Prefer the simplest design that works: a single well-prompted call beats a chain; a chain
   with code-controlled steps beats an autonomous agent. Reach for agents only when the steps
   genuinely cannot be known in advance.

## Prompts

- System prompt = stable role, context and rules; the user turn = the task and its data. Put
  long documents first, then the instructions and the question.
- Be specific and positive: say what to do and why, give the output format, and include one
  or two examples when format or tone matters. Remove instructions that no longer apply.
- Separate untrusted content (user input, retrieved documents, tool results, web pages) with
  clear tags such as `<document>` and tell the model to treat it as data, never instructions.
- Keep prompts in version-controlled code or files, reviewed like code.

## Calling the model

- Always check `stop_reason` before using the output: `end_turn` (done), `tool_use` (run tools
  and continue), `max_tokens` (truncated: do not use a partial tool call or JSON), `refusal`
  (declined: show a graceful message), `pause_turn` (server tool paused: re-send to resume).
- Stream anything user-facing or long; forward text deltas to the browser (SSE) and send the
  final message to logs. Set `max_tokens` generously for streaming and accordingly for
  non-streaming requests to stay within HTTP timeouts.
- For thinking-capable models use adaptive thinking and tune `output_config.effort` instead of
  fixed budgets; lower effort for simple, high-volume routes.
- Assistant-message prefill is not supported on current models; use structured outputs or
  instructions to control the format instead.
- Rely on the SDK's retries (429 / 5xx / connection errors) with a sensible `max_retries` and
  timeout; catch the SDK's typed exceptions (`RateLimitError`, `APIStatusError`,
  `APIConnectionError`) rather than string-matching messages.

## Tool use and agents

- Tools have clear names, a description saying when to use them, and strict JSON schemas
  (`strict: true` with `additionalProperties: false` when inputs must validate exactly).
- Execute every tool call from a response and return all `tool_result` blocks in one user
  message; failures return `is_error: true` with a message the model can act on.
- Validate tool inputs as untrusted: paths, IDs and SQL come from the model. Gate anything
  with side effects (writes, payments, emails) behind confirmation or strict allow-lists.
- Append the full assistant `content` (including thinking and tool_use blocks) to history
  unchanged; never edit earlier turns.
- Cap loop iterations and total tokens; log every step so a run can be replayed.

## Agent loop engineering

The loop is: call the model -> if `stop_reason == "tool_use"`, run the tools and send the
results -> repeat until `end_turn`. Everything else is making that loop reliable.

- **History is append-only.** Append each assistant `content` exactly as returned (thinking,
  text, tool_use, server tool blocks) and never rewrite or delete earlier turns; editing
  history breaks prompt caching and can invalidate thinking blocks.
- **One user message per tool round**, containing a `tool_result` for every `tool_use` id in
  the previous response, in any order, with `is_error: true` for failures.
- **Handle every stop reason:** `pause_turn` -> re-send the history unchanged to resume;
  `max_tokens` with a tool_use present -> never run the truncated call; `refusal` -> stop and
  tell the user.
- **Bound the loop:** maximum iterations, a token/cost budget, and per-tool timeouts. Detect
  repetition (same tool + same input several times) and stop or ask the human.
- **Keep the tool set and system prompt fixed for a session** (sorted, deterministic); changing
  them mid-conversation invalidates the cache. Express "modes" as messages or tool arguments.
- **Keep context small:** truncate large tool outputs with a note saying so, return summaries
  or line ranges instead of whole files, and for long sessions use context editing (clear old
  tool results) or compaction (beta on Foundry) rather than letting the window fill.
- **Tool results should help the next step:** say what happened, what was found, and on error
  what to try instead ("old_string not found; re-read the file"), not a bare stack trace.
- **Human in the loop:** require confirmation for side effects (writes, deploys, messages),
  show diffs before writing, and pass the user's rejection reason back as the tool result.
- **Make runs debuggable:** log each iteration (stop_reason, tool names and inputs, durations,
  usage), save the transcript so a run can be resumed or replayed, and build evals from real
  transcripts (did it pick the right tools, in a reasonable number of steps?).
- Start with a manual loop when you need approval gates, streaming UI or custom persistence;
  the SDK's tool runner is fine for simple cases.

## MCP (Model Context Protocol)

MCP standardizes how an application exposes tools, resources and prompts to a model. A server
offers them; a client (your app, Claude Desktop, an IDE) connects, lists them and calls them.

Building servers (Python `mcp` SDK, `FastMCP`):
- One server per coherent domain; a few well-designed tools beat many thin wrappers over
  endpoints. Name tools as actions (`search_orders`), write descriptions that say when to use
  them, and type every parameter (type hints + docstrings become the schema).
- Return concise, model-readable text (or structured content): the facts needed for the next
  step, not raw API dumps. Paginate or cap large results and say so.
- Raise clear errors the model can recover from; validate inputs as untrusted.
- stdio transport: never write to stdout except through the protocol (it corrupts the JSON-RPC
  stream); log to stderr. Use Streamable HTTP for remote/shared servers, with authentication
  (OAuth for user-facing servers) and per-user authorization inside tools.
- Test with the MCP Inspector (`npx @modelcontextprotocol/inspector`, run by the user) and
  with unit tests that call the tool functions directly.

Consuming servers:
- From your own code: open a `ClientSession`, `list_tools()`, convert each tool to an
  Anthropic tool definition (name, description, `inputSchema` -> `input_schema`), and in the
  loop route `tool_use` calls to `session.call_tool` and its content back as the tool_result.
  Keep the session open for the whole conversation and close it on exit.
- Remote servers can instead be attached server-side with the Claude API MCP connector
  (`mcp_servers` + an `mcp_toolset` tool, beta header `mcp-client-2025-11-20`; beta on Foundry).
  It supports remote URL servers only, not local stdio servers.
- Treat MCP tool output as untrusted input (prompt injection); do not give a third-party
  server tools with side effects on your systems without confirmation.

## Skill engineering (writing SKILL.md files)

A skill is a folder with a `SKILL.md`: a header with `name` and `description`, then the
instructions. Only the description is shown up front; the body is loaded when a task matches.

- **The description decides whether the skill is used.** State what it covers and when to use
  it, with the concrete words users and tasks contain (frameworks, file types, verbs). Vague
  descriptions ("helps with backend") are loaded at the wrong times or never.
- **Write a playbook, not a persona.** One line of role, then the procedure (how to start, what
  to check first), concrete defaults and decisions, pitfalls, and a review checklist. Every line
  should change behavior; cut general advice the model already follows.
- **Be specific to this environment:** the project's stack, the agent's actual tools and their
  limits (e.g. cannot run npm or deploy), and what to hand back to the user.
- **Keep it focused and short** (roughly under 300 lines); split unrelated areas into separate
  skills and cross-reference them by name instead of duplicating content.
- **Explain the why** behind non-obvious rules so the model can apply them to cases you did not
  list; prefer "do X" over "don't do Y".
- **Test it:** run a few real tasks and check that the skill is loaded when it should be, not
  loaded when it should not, and that the output follows it; revise from what you observe.
- Version skills in git (project skills in `<project>/.agent/skills/`) and review changes like
  code. In this agent, load_skill returns only SKILL.md, so keep everything in that one file.

## Structured output

- Use the API's structured outputs (`output_config.format` with a JSON schema) or a strict
  tool when you need machine-readable results; validate with Pydantic on receipt anyway.
- Keep schemas small and flat; enumerate allowed values; make "unknown / not found" an explicit
  option so the model does not invent data.

## Retrieval and indexing

Retrieval quality caps answer quality: if the right passage is not in the top results, no
prompt fixes it. Measure retrieval on its own before tuning the generation step.

**Lexical search (BM25).** Scores documents by query-term matches: rarer terms weigh more
(IDF), repeated terms saturate (`k1`, typically 1.2-2.0; 1.2 in Lucene/Elasticsearch/Azure AI
Search), and long documents are normalized (`b`, typically 0.75; lower it when length does
not mean dilution, e.g. code or specs).
- Strong on exact terms: product codes, error messages, IDs, names, rare jargon; cheap,
  fast, explainable, no embeddings to maintain.
- Weak on vocabulary mismatch: synonyms, paraphrases, cross-language queries.
- The analyzer matters as much as the formula: language analyzers (stemming, stop words) for
  prose, keyword/n-gram analyzers for codes and partial matches, synonym maps for domain terms,
  field boosts (title > body).
- PostgreSQL `tsvector`/`ts_rank` is full-text search but not BM25 (no IDF, no term saturation);
  BM25 in Postgres needs an extension (check it is allowed on Azure Database for PostgreSQL).
  For small or offline corpora, the `rank_bm25` Python package is enough.

**Vector search (dense embeddings).** Matches meaning; handles paraphrase and multilingual
queries; weak on exact identifiers, numbers and out-of-domain terms.
- Pick the embedding model by evaluating it on your queries; store the model name and
  dimensions with every vector and re-embed everything when the model changes.
- ANN indexes (HNSW) trade recall for speed: raise `efSearch`/`ef_construction`/`m` for recall,
  lower for latency and memory. Use exhaustive k-NN as ground truth for small sets and to
  measure the ANN recall loss. Quantization cuts memory at some recall cost; measure it.

**Hybrid search.** Run BM25 and vector search and fuse the ranked lists, usually with
Reciprocal Rank Fusion (RRF, `score = sum 1/(k + rank)`, k around 60), which needs no score
normalization; Azure AI Search hybrid queries use RRF. Hybrid is the safe default for mixed
query types.

**Reranking.** Retrieve a wide candidate set (e.g. top 50), then rerank with a cross-encoder,
the Azure AI Search semantic ranker, or an LLM, and keep the top 5-10 for the prompt. Usually
the biggest single quality gain after hybrid.

**Chunking and enrichment.** Split by document structure (headings, sections, functions),
around 200-800 tokens with modest overlap; keep title, section path, source and dates as
metadata; prepend a short context header (document title + section) to each chunk before
embedding so chunks stay meaningful on their own.

**Query side.** Apply metadata filters (tenant, permissions, date, type) inside the search,
not after it, so results are both correct and complete; enforce access control at retrieval
time. Consider query rewriting or multi-query for conversational follow-ups.

**Grounding.** Put retrieved chunks in the prompt with source IDs, ask for answers grounded in
them with citations, and allow "not found in the documents" instead of guessing.

**Evaluating retrieval.** Build a labeled set of real queries with their relevant documents;
report Recall@k (did the right chunk make the top k?), MRR or nDCG@10 (how high?), and p95
latency. Compare configurations on the same set: BM25 vs vector vs hybrid vs hybrid + reranker,
chunk sizes, analyzers, embedding models.

**Which engine** (then confirm with the evaluation above):

| Situation | Start with |
|---|---|
| Production search on Azure, mixed queries, need filters and security trimming | Azure AI Search: BM25 + vector hybrid (RRF) + semantic ranker |
| Already on PostgreSQL, modest corpus, want one database | pgvector (HNSW) + Postgres full-text, fused with RRF in SQL |
| Mostly exact lookups: codes, logs, error messages, source code | BM25 alone (Azure AI Search or an Elasticsearch/OpenSearch cluster you already run) |
| Small, static or offline corpus, prototypes, evals | In-process: `rank_bm25` + a local vector index |
| Multilingual or paraphrase-heavy natural-language questions | Hybrid with a strong multilingual embedding model + reranker |

## Choosing between solutions

When there are competing options (search engines, models, architectures, libraries), act as
the arbiter: make the decision explicit, evidence-based and reversible.

1. **Frame it:** the problem, the constraints (latency, cost, data residency, team skills,
   existing stack), and what "good" means in measurable terms.
2. **Shortlist 2-3 real options**, including "keep what we have" and the simplest option.
3. **Score them** on weighted criteria: quality on our own data, latency, cost at expected
   volume, operational burden, security/compliance, lock-in and migration cost, team
   familiarity. Weights come from the constraints, not preference.
4. **Measure, don't argue:** when quality decides, run a small benchmark on real data (e.g.
   50-200 labeled queries) with each option; report the numbers with their uncertainty.
5. **Recommend one** with the main trade-off in a sentence, what would change the decision,
   and how to reverse it. Record it as a short decision record (context, options, decision,
   consequences) in the repo when the choice is significant.
6. **Hand disagreements back with evidence:** when options tie or the trade-off is the user's
   (cost vs quality, speed vs control), present the numbers and ask with ask_human instead of
   picking silently.

## Cost and latency

- Prompt caching: keep the prefix (tools, system prompt, long shared context) byte-identical
  across requests, put volatile content (timestamps, user data) after it, and confirm hits with
  `usage.cache_read_input_tokens`. Changing tools or the system prompt invalidates the cache.
- Count tokens with the API's token counter, not a third-party tokenizer.
- Pick the smallest model and lowest effort that pass the evals; cap `max_tokens`; batch
  offline work.
- Log `usage` (input, output, cache read/write tokens) per request and per feature.

## Evaluation

- Keep an eval set in the repo: realistic inputs (including edge cases and adversarial ones)
  with expected outcomes or grading rubrics. Grow it from real failures.
- Grade with code where possible (exact match, schema validity, contains citation); use an
  LLM judge with a precise rubric for open-ended output, and spot-check the judge.
- Run evals before merging prompt, model or retrieval changes and compare against the last
  baseline; report pass rate, cost and latency together.

## Safety and privacy

- Treat all model output as untrusted: escape it before rendering as HTML, never execute it
  without validation, and never let it choose which user's data to access.
- Defend against prompt injection from retrieved or user content: least-privilege tools,
  confirmation for side effects, and no secrets in prompts.
- Do not log secrets; minimize PII in prompts and logs, and follow the data-retention policy.
- Handle refusals and content-filter responses with a clear message to the user, not a crash.

## Observability

- Log per request: request ID, deployment/model, prompt version, latency, `stop_reason`,
  token usage, tool calls and errors (not raw PII). Trace multi-step flows end to end
  (OpenTelemetry / Application Insights).
- Alert on error rate, refusal rate, 429s, latency and spend per feature.

## Review checklist

- [ ] Model / deployment and prompts come from configuration or versioned files
- [ ] Every `stop_reason` handled; partial output never used as complete
- [ ] Streaming for user-facing responses; timeouts and retries set
- [ ] Untrusted content separated and tool inputs validated; side effects gated
- [ ] Structured output validated on receipt
- [ ] Cache-friendly prompt layout; usage and cost logged
- [ ] Evals exist for the feature and pass against the baseline
- [ ] Agent loops are bounded, append-only, and handle every stop reason
- [ ] MCP servers log to stderr (stdio), return concise results, and authorize per user
- [ ] Retrieval measured on labeled queries (Recall@k, nDCG) before and after changes
- [ ] Significant technical choices compared on weighted criteria with measurements
