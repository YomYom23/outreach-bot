# Vendor notes (Task 0)

Checked 2026-11-13 against the design's Task 0 list (`docs/designs/outreach_bot.md`, "Task 0: Verify vendor facts before coding against them").

**How each fact was checked**

- **probe**: a local script ran against the installed library with no network and no API keys. LangGraph ran real graphs. `ChatAnthropic` ran against a local stub of `/v1/messages` that returns success, `refusal` and `max_tokens` replies.
- **source**: read in the installed package source.
- **docs**: the vendor's current documentation page, linked.
- **pending 3b**: needs a live key. These are measured by the phase 3b probe and added to this file then.

**Result codes**

- **OK**: matches the design.
- **OK\***: matches, with a detail the code must follow.
- **CHANGE**: the design has to change.
- **PENDING**: not checked yet.

## Versions checked

| Package | Version |
|---|---|
| langgraph | 1.2.21 |
| langgraph-prebuilt | 1.1.1 |
| langgraph-checkpoint | 4.3.1 |
| langgraph-checkpoint-sqlite | 3.1.3 |
| langchain-anthropic | 1.7.10 (with langchain-core 1.8.0) |
| anthropic | 2.8.0 |
| langsmith | 0.14.17 |
| exa-py | 2.28.0 |
| tldextract | 5.4.0 |
| email-validator | 2.3.0 |
| phonenumbers | 9.0.43 |
| pytest-socket | 0.8.1 |

**Minimum pins:** `langchain-anthropic>=1.7.10`, `langgraph>=1.2.21`.

- 1.7.10 is the version verified here. It knows that Opus 5.5 rejects forced `tool_choice` (`_supports_forced_tool_choice`). Older releases may not.
- The exact first release with this support was not checked against the changelog.

## Anthropic / langchain-anthropic

| Fact | Result | Evidence |
|---|---|---|
| `with_structured_output(schema, method="json_schema", include_raw=True)` works on `claude-opus-5-5` and `claude-haiku-5-5` without forcing `tool_choice` | OK | probe: the request carries `output_config.format = {type: json_schema}` and has no `tool_choice` key, on both models. The result is parsed into the Pydantic model. |
| Default `method="function_calling"` on Opus 5.5 | OK\* | source + probe: on Opus 5.5 the library skips the forced call and warns. On Haiku 5.5 it **does** send `tool_choice: {type: tool}`. Always pass `method="json_schema"`, as the design says. |
| Effort per instance | OK | probe: `ChatAnthropic(effort="low")` is sent as `output_config.effort`. `reasoning_effort=` is the canonical field name and `effort=` is an alias. |
| Opus 5.5 thinking setting | OK\* | probe: when `thinking` isn't set, the library sends `thinking: {type: "adaptive", display: "summarized"}` on Opus 5.5 and sends nothing on Haiku 5.5. `{type: "disabled"}` raises client-side on Opus 5.5. Don't set `thinking`. Use effort only. |
| Refusal and `max_tokens` show up on the raw message | OK\* | probe: `result["raw"].response_metadata["stop_reason"]` is `"refusal"` or `"max_tokens"`, and `response_metadata["stop_details"]` holds `{type, category, explanation}` on a refusal. `parsed` is `None`. `parsing_error` is `ModelRefusalError` on a refusal and a parser error on truncation. `llm.py` must branch on `raw.response_metadata["stop_reason"]`, not on the exception type. |
| Thinking tokens count against `max_tokens` | OK (docs) / PENDING 3b | Per the Anthropic docs, `max_tokens` caps all output, thinking included, and `usage.output_tokens` includes thinking. The real share at each call site's effort is measured in 3b. |
| Measured tokens per call site | PENDING 3b | Needs a key. These numbers resize the `[max_tokens]` table. |
| Thinking blocks kept across tool turns | OK | source: when it formats an `AIMessage` it re-emits `thinking` blocks with their `signature` (`chat_models.py`, the "thinking" branch). The discovery loop appends the `AIMessage` unchanged, which is all it needs. |
| `max_retries` on `ChatAnthropic` | OK | source/probe: field `max_retries: int = 2`, passed to the SDK client. |
| Forced `tool_choice` returns 400 on Opus 5.5 | OK | claude-api skill (model notes, cached 2026-11-03). The library also guards it. |
| Prices | OK | claude-api skill, cached 2026-11-03: Opus 5.5 is $4 in / $20 out per MTok (cache reads $0.20). Haiku 5.5 is $0.10 / $0.50 per MTok for prompts up to 100K tokens, and $0.50 / $2.50 above. Opus 5.5 and Haiku 5.5 both **default to effort `medium`**, so set effort explicitly at every call site. |

## LangGraph

| Fact | Result | Evidence |
|---|---|---|
| A resumed node re-runs from its first line | OK | probe: a node with 2 `interrupt()` calls was entered 3 times, once at the start and once per resume. Code before an `interrupt()` must be idempotent. |
| Several `interrupt()` calls in one node: how resume values are matched | OK | probe: by order. The first resume answers `q1`. On the next pass `q1` replays its stored value and `q2` pauses. The final result was `"A,B"`. |
| `get_state(config).tasks[*].interrupts` | OK | probe: holds `Interrupt(value=..., id=...)`. |
| `Command(goto=...)` can reach any node | OK | probe: `Command(goto="far")` skipped `mid`. Declare `destinations=` on `add_node` so the graph drawing shows the edges. |
| `stream(..., durability="sync")` | OK | probe: accepted, and the run completed. |
| `recursion_limit` in the run config, and whether it resets per `stream()` | OK | probe: the step count resets on every `invoke`/`stream`. Two calls of 4 steps each both passed under a limit of 6. |
| Do subgraph steps count toward the parent's limit? | OK\* | probe: **no**. A subgraph invoked inside a node gets the **same limit value** through the inherited config, but it counts **its own** steps from 0. A parent with 5 steps plus a subgraph with 7 steps passed under a limit of 9. A subgraph with 8 steps failed under a limit of 5. The design's `recursion_limit` formula therefore over-counts, which is safe: it only has to exceed the larger of the parent's own steps and one subgraph invocation's steps. |
| Default `recursion_limit` | info | source: `DEFAULT_RECURSION_LIMIT` is 10007, not 25, and can be overridden with an environment variable. The design always sets it explicitly, so nothing depends on the default. |
| `get_stream_writer()` and the event shape for a list `stream_mode` with `subgraphs=True` | OK | probe: events are 3-tuples `(namespace, mode, chunk)`. A custom event written inside a subgraph that a node invokes arrives as `(("w2:<task-id>",), "custom", {...})`. Parent events have the namespace `()`. |
| `SqliteSaver(conn)` with `setup()`, and `delete_thread` | OK | probe: `SqliteSaver(sqlite3.connect(..., check_same_thread=False)).setup()` works, and `delete_thread(thread_id)` removes every checkpoint for that thread. |
| Checkpoint serializer types | OK\* | probe: dicts round-trip. Pydantic models and Enums **also** round-trip, but log *"Deserializing unregistered type ... will be blocked in a future version"*. Keep checkpointed values JSON-native, as the design already does: `ParentState` holds `str` and `dict`, and resume values are `answer.model_dump(mode="json")`. Don't put Pydantic or Enum objects in state or in `Command(resume=...)`. |
| `compile(checkpointer=False)` for subgraphs | OK | probe: compiles, and runs inside a checkpointed parent. |
| `ToolNode([...], messages_key="discovery_messages")` | OK | probe: reads tool calls from that key and writes `ToolMessage`s back to it. `tools_condition(state, messages_key=...)` also accepts the key. |
| ToolNode needs a graph context | OK\* | probe: `ToolNode.invoke(...)` called on its own outside a graph raises `Missing required config key 'N/A'`. Unit tests must run it inside a small compiled graph, not invoke it bare. |
| A tool reads an injected `RunnableConfig` | OK | probe: `def exa_search(query: str, config: RunnableConfig)` received `configurable.run_id` and `configurable.round`, and the model-visible schema contains only `query`. |
| `langgraph-supervisor` claim (Alternative D) | OK | [repo README](https://github.com/langchain-ai/langgraph-supervisor-py): handoff tools pass the supervisor's messages along a shared `messages` list (`output_mode` is `full_history` or `last_message`). The repository is also **archived (read-only)** and recommends building the supervisor pattern directly with tools. That is a stronger reason for rejecting it. |

## Exa

Sources: [Exa search reference](https://exa.ai/docs/reference/search), and the `exa-py` 2.28.0 source.

| Fact | Result | Evidence |
|---|---|---|
| `num_results` and its maximum | OK | docs: default 10, range 1–100. |
| `exclude_domains` and its size limit | OK | docs: up to **1200** entries. Each entry is a hostname, a hostname with a path prefix, or a wildcard subdomain. Trim the blocklist to 1200. |
| `category="company"` | OK\* | docs: `company` accepts `excludeDomains` but **rejects** `startPublishedDate`/`endPublishedDate` with a 400. (`people` also rejects `excludeDomains`, which doesn't affect this project.) |
| `highlights={"num_sentences": 2}` | **CHANGE** | docs: `numSentences` is **deprecated** and maps to about 1,333 characters per sentence. `highlightsPerUrl` is ignored. Use `highlights={"max_characters": N}`. N ≈ `snippet_chars`-scale, and the suggestion is 400. |
| The snake_case cost field | OK | source: `exa-py` parses `costDollars` into `response.cost_dollars`, a `CostDollars(total, search, contents)`. Record `cost_dollars.total`. The docs call it an estimate, "not an invoice record". |
| `search_and_contents` signature | OK\* | source: `(query, **kwargs)`, so typos in argument names are not caught by Python. Pin the kwargs in a unit test against a recorded request. |
| Cost per call | OK (measured) | $0.022 per search (25 results, highlights, `type=auto`, `objective`, `category=company`), from `cost_dollars.total` on all 15 probe calls, 2026-11-13. |

## Hunter

Source: [Hunter API v2 documentation](https://hunter.io/api-documentation/v2).

| Fact | Result | Evidence |
|---|---|---|
| Domain Search fields | OK\* | Each email has `value`, `type` (`personal` or `generic`), `confidence`, `first_name`, `last_name`, `position`, `seniority` (`junior`, `senior` or `executive`), `department` and `sources[]`. **The source URL field is `sources[].uri`**, not `url`. Each source also has `domain`, `extracted_on`, `last_seen_on` and `still_on_page`. |
| `limit` on the free plan | **CHANGE** | `limit` defaults to 10 and has a maximum of 100. On free plans, `limit + offset > 10` returns `pagination_error`. Use `limit=10`. |
| Account and credits endpoint | OK | `GET /v2/account` (charges no credits) returns `requests.credits`, `requests.searches` and `requests.verifications`, each with `{used, available, remaining}`, plus `reset_date`. The free-tier allowance isn't stated in the docs, so read it from this endpoint at runtime. |
| Credit charge | info | A search counts only when it returns at least one result. Repeating a search in the same billing period reports `X-Credits-Used: 0`. |
| Error codes, including quota | **CHANGE** | **`429` = usage limit reached (out of quota)**. **`403` = rate limit reached.** 401 means a bad key. No error id is documented for quota. The design's error policy retries "429/5xx" with `tenacity` for every vendor. For Hunter that would retry quota exhaustion and never retry the real rate limit. |
| Rate limits | info | Domain Search allows 15 requests per second and 500 per minute. |

## Mailtrap (Email Sandbox)

Sources: the [Mailtrap pricing page](https://mailtrap.io/pricing.md) and Mailtrap's SMTP port docs. The sandbox docs pages for host and port returned 404 today.

| Fact | Result | Evidence |
|---|---|---|
| Host | OK\* | `sandbox.smtp.mailtrap.io`. The exact host and credentials are shown in the sandbox's **Integration → SMTP** tab. Copy them from there into `.env` and `mail_host_allowlist`. |
| Port and STARTTLS | OK | Ports 25, 465, 587 and 2525. STARTTLS works on all of them, and 2525 is recommended for compatibility. Use 2525 with STARTTLS and LOGIN or PLAIN auth. |
| Free plan limits | **CHANGE** | **1 email per sandbox per 10 seconds**, **50 test emails a month**, and **10 emails kept** per sandbox (oldest removed first), with a 5 MB maximum size. Third-party pages quote 100 or 500 a month. The official pricing page says 50. |

## LangSmith

Sources: [Trace with LangGraph](https://docs.langchain.com/langsmith/trace-with-langgraph), [Threads](https://docs.langchain.com/langsmith/threads), [pricing](https://www.langchain.com/pricing).

| Fact | Result | Evidence |
|---|---|---|
| `LANGSMITH_TRACING` | OK | `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY`. `LANGSMITH_ENDPOINT` is needed for non-US accounts. Destination: `LANGSMITH_AGENT_ID=outreach` + `LANGSMITH_AGENT_ENVIRONMENT=local` (2026-11-13). Setting `LANGSMITH_PROJECT` as well makes the SDK trace nothing (`EnvAddressError`: neither outranks the other), so it is left unset. Also fixed 2026-11-13: `make_runtime` checked for the key before `.env` was loaded and forced `LANGSMITH_TRACING=false`. |
| Thread grouping | OK\* | Runs are grouped by a metadata key named **`session_id` or `thread_id`**, not `conversation_id`. Put `thread_id=run_id` in `config["metadata"]`. |
| Metadata and `run_name` on sub-invokes | OK (source) / PENDING 3b | Both are standard `RunnableConfig` fields that pass to child runs. Confirm they render in the UI on the first traced run. |
| Flushing | PENDING 3b | Use `langsmith`'s `wait_for_all_tracers()` (from `langchain_core.tracers.langchain`) before the CLI exits, and confirm that the last run appears. |
| Free-tier retention | OK | Developer plan: 5,000 base traces a month, kept for **14 days**. Extended traces are kept for 180 days and cost extra. |

## Libraries

| Fact | Result | Evidence |
|---|---|---|
| `tldextract` bundled snapshot with `suffix_list_urls=()` | OK\* | probe: works offline. Two details matter. (1) It does **not** lowercase, so lowercase before extracting, as `normalize_domain` already does. (2) With `include_psl_private_domains=True`, hosts such as `joe.wixsite.com` and `bob.github.io` become **their own** registrable domain (`is_private=True`). The shared-host skip has to test the **suffix**, or `is_private`, against `shared_host_patterns`, not the registrable domain. Use `.top_domain_under_public_suffix`. `.registered_domain` is the older name. |
| `email-validator` with `check_deliverability=False, test_environment=True` | OK | probe: `owner@acme.test` is accepted with `test_environment=True` and rejected without it ("special-use or reserved name"). |
| Cloudflare `data-cfemail` XOR format | OK | probe: the first hex byte is the key, and every later byte is XORed with it. Round-trip passed. |
| `pytest-socket` | OK | Exports `disable_socket`, `enable_socket` and `socket_allow_hosts`. Use `--disable-socket --allow-hosts=127.0.0.1` in `pytest.ini`. Tests that start a local stub server need the localhost exemption. |
| `phonenumbers` US evidence | OK\* | probe: fictional `555-01xx` numbers pass `is_valid_number` with region `US`. A placeholder number on a template site would count as US evidence. This is accepted as low risk and noted only. |

## Claims in earlier sections

| Claim | Result | Evidence |
|---|---|---|
| Alternative F: page content from Claude's web search comes back encrypted | OK | [Anthropic web search docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool): a `web_search_result` has only `url`, `title`, `page_age` and `encrypted_content`. Citations carry at most 150 characters of `cited_text`. The price is $10 per 1,000 searches. |
| The cost figures in The Problem and Alternative E | deferred | Rechecked at the phase 7 exit, as planned. |

## Design changes needed (from the CHANGE rows)

**Status: applied to `docs/designs/outreach_bot.md` on 2026-11-13**, and the `../egm/design_draft.md` mirror was regenerated. The edited places are: the Error policy rows for Hunter, the Rules note on Hunter fields, items 33 (`domains.py`), 44 (`exa_client`), 45 (`hunter_client`) and 46 (`mailer`), and the `[mail]` config row.

1. **Hunter errors.**
   - Map `429` to `HunterQuotaError` before `tenacity` sees it.
   - Treat Hunter `403` as the rate-limit response, and retry it with backoff.
   - Affects: the error-policy rows for Hunter and the retry predicate in `hunter_client`.
2. **Hunter `limit=10`** on Domain Search. Read `sources[].uri` for the source URL.
3. **Exa highlights.**
   - Replace `highlights={"num_sentences": 2}` with `highlights={"max_characters": 400}`.
   - Trim `exclude_domains` to 1200.
   - Never send published-date filters with `category="company"`.
4. **Mailtrap pacing.**
   - The mailer must wait at least 10 seconds between sends, through a new `[mail] min_send_interval_s = 10`. Without this, a second send fails, and the error policy marks the draft `failed` with no retry.
   - The free inbox keeps only the last 10 messages, and the plan allows 50 a month. The live manual check should use a small target of 10 or fewer.
5. **Shared-host check in `normalize_domain`.** Match `shared_host_patterns` against the public suffix, or `is_private`, because the private PSL turns `joe.wixsite.com` into its own registrable domain. Phase 4: `normalize_domain` matches the patterns against the whole host, because some site builders are missing from the private list: `x.squarespace.com` would otherwise reduce to `squarespace.com` and merge every Squarespace site into one lead.

Smaller notes to carry into the code, not into the design:

- `llm.py` branches on `raw.response_metadata["stop_reason"]`.
- Don't set `thinking` on Opus 5.5 or Haiku 5.5. Set effort explicitly, because the default is `medium`.
- Unit tests run `ToolNode` inside a graph.
- Keep checkpointed values JSON-native.
- Use `thread_id` in LangSmith metadata.

## Phase 3b: clients built (2026-11-13)

Found while building `exa_client` and `hunter_client` against exa-py 2.28.0 and httpx. Applied to design item 44.

| Fact | Result | Evidence |
|---|---|---|
| `search_and_contents` is deprecated | **CHANGE** | source: exa-py 2.28 marks it deprecated; `search()` now returns contents itself. With no `contents`, it returns up to 10,000 characters of page text per result. `exa_client` calls `search(query, type="auto", num_results=…, objective=…, category="company", exclude_domains=…, contents={"highlights": True})`. These are typed keyword arguments, so typos fail in Python. `test_exa_client` pins the request body (`type`, `numResults`, `objective`, `category`, `contents.highlights`, `excludeDomains` trimmed to 1200, and no `text` or published-date fields) against the real exa-py. |
| Exa's build-with-exa skill (v0.3.0, read 2026-11-13) | **CHANGE** | Exa's own guidance for agents ([exa-labs/agent-skills](https://github.com/exa-labs/agent-skills), `skills/build-with-exa`). (1) Send bare `highlights: true`; `maxCharacters` only with an explicit budget. Our budget, `snippet_chars`, is applied in code, so the 400-character cap was dropped. (2) Send `objective` on every search that is one step of a larger task. When an LLM calls search as a tool, `objective` is a required tool parameter next to `query`, with the skill's description used verbatim (`exa_client.OBJECTIVE_DESCRIPTION`), passed to `search()` unchanged. Applied to the probe, to design item 31's `exa_search` tool and to `prompts/discovery.md`. (3) Use `category` and `excludeDomains` only when they are explicitly wanted. Both are explicit design decisions here, so they stay. The skill also says people and company list-building belongs on its Agent API (`/agent`). This project uses `/search` to find raw company sites for its own pipeline, which the skill allows. Whether `category="company"` helps or hurts the number of own sites found is measured by running the probe with and without `--no-category`. |
| Exa HTTP errors | OK\* | source: exa-py raises a plain `ValueError("Request failed with status code NNN: …")`, and timeouts raise `ExaTimeoutError`. `exa_client` parses the status from that message. 401 and 402 map to `VendorAuthError`. 429, 5xx and connection errors are retried `vendor_retry_attempts` times and then raise `VendorUnavailableError`. Other 4xx errors are not retried. **`retry-after` can't be honoured for Exa**, because exa-py exposes no response headers, so retries use exponential backoff instead. |
| Hunter authentication | info | `hunter_client` sends the key as the documented `api_key` query parameter, over HTTPS only. Error messages never include the URL. |
| Hunter credits per call | OK\* | `hunter_client` records `X-Credits-Used` when the header is present. Otherwise it records 1 credit if the search returned emails, else 0. |

### Probe results

**Decision: GO** (2026-11-13; full table in [probe_3b_results.md](probe_3b_results.md)).

- 15 searches (5 wordings x Tucson, Phoenix, Austin), 25 results each, `category=company`: an average of **25.0 own sites per search** against a threshold of 5. 134 distinct domains, all labelled `contractor_own_site` by Haiku triage.
- A manual spot-check of one search ("roofing contractor in Phoenix, AZ") found 24 clear roofers' own sites and 1 unclear one (`global.inc`, "Global Group"). The labels are right, but triage never said no in this run, so its precision on harder results is still untested. The phase 4 injection and negative-case tests need to cover it.
- 16-22 of the 25 own sites per search showed an email on the home or contact page (average about 18). Up to about a third are form-only, which is where Hunter is meant to help.
- Exa: $0.022 per search. Triage (Haiku): about 808 input and 65 output tokens per call on average, 86 at most.
- Hunter: not run (`HUNTER_API_KEY` is empty). Run the probe with `--hunter 10` once the key is set.
- `--no-category` was not run: with 25/25 there is nothing for it to improve.
- Local environment: Norton Antivirus intercepts TLS on this machine. Its `SSLKEYLOGFILE` named pipe crashes OpenSSL ("no OPENSSL_Applink"), and its root certificate isn't in certifi. The probe ran with `SSLKEYLOGFILE` unset and `truststore.inject_into_ssl()`. Both are now in `outreach/tls.py`, called at the start of the CLI and the probe. The machine-level fix is a Norton exclusion for Python's HTTPS traffic.

## Phase 4: researcher findings

| Item | Status | Notes |
|---|---|---|
| `.test` fixture domains and tldextract | **CHANGE** | `.test` isn't in the public suffix list, so every fixture domain normalized to "unparseable". `normalize_domain` passes `extra_suffixes=("test",)`. `.test` is reserved (RFC 2606) and never resolves publicly, so production behaviour is unchanged. |
| Shared hosts missing from the private PSL | **CHANGE** | See the tldextract item above: patterns are matched against the whole host. |
| The quote check's limit | Noted | `quote_found` proves a quote is on the page, not that it's true. An attacker can write "Bob Smith, owner since 1950" on their own page, and a judge that repeats it passes. Code-enforced outcomes still hold: the attacker's off-domain address is `other_domain`, never on-site (`test_injection_research`). Phase 5's owner match must keep relying on on-site, same-domain or freemail addresses, never on a quote alone. |
| Discovery stops before the model can overrun | OK | `discover` checks `max_exa_searches_per_round` and `max_companies_examined_per_round` before each model turn, so code ends the loop. The tool also refuses on its own if called past the limit. |
| `exa_dollars_per_call` | OK | Set to the measured $0.022 in `config/settings.toml`. |
| Exa's company library in results | **CHANGE** | Live round (2026-11-13, `docs/phase4_live_round.md`): with `category="company"`, Exa also returns its own profile pages (`exa.ai/library/organization/<id>`), titled with a roofer's name. Triage labelled `exa.ai` an own site, because the snippet describes one company. `exa.ai` is now in `directory_blocklist`, so code skips it. (Phase 7: it is no longer sent to Exa as an exclude; see below.) The phase 3b probe counted it as 1 of its 134 domains. |
| Live research round | OK | Tucson: 10 counted companies in 160 s for about $0.06 (6 searches, 22 triage, 10 judge calls). 6 candidates with on-site email (one Gmail), 4 `parked_no_email`. All 4 parked sites have no address in their HTML, only contact forms, so they are phase 5's Hunter cases, not extraction misses. 2 fetch failures (1 JS shell, 1 HTTP error). Discovery queued 22 domains for a batch of 10; the 12 left over are carried into round 2. |

## Phase 5: qualifier findings

| Item | Status | Notes |
|---|---|---|
| Business-named addresses | **CHANGE** | Live qualification (`docs/phase5_live_qualify.md`): Ridgeline LLC's only address is `ridgelineaz@gmail.com`. It isn't on the role list, isn't an owner name and isn't name-shaped, so under the design's rules it could never be chosen. The Rules now also count as general an address whose local part contains the company's name (filler words such as "Roofing" and "LLC" removed, at least 4 letters), unless it is a staff personal address. |
| Staff "name-shaped" test | Decision | Only `first.last` and `first_last` shapes count as name-shaped. A looser test (any word) would make `info@` look like a person. Local parts matching a non-owner person's name forms are always staff. |
| Hunter cap counting | Decision | The per-run cap counts `api_calls` rows with `call_site = domain_search` for the run. A `hunter_cache` hit costs nothing and doesn't count. Credits are read once per qualify pass from `account()`, then decremented locally. `FakeHunter` records the same rows. |
| Hunter without a key | Decision | `make_runtime` builds no Hunter client when `HUNTER_API_KEY` is empty. Form-only leads at or above N then end as `no_email` (not `hunter_quota`), and the run still works. |
| Live qualification | OK | The 10 counted Tucson companies, N = 2: 6 qualified, all with a general on-site address. 4 `no_email` are the form-only sites, waiting on Hunter. 2 sites show an AI chat marker (`no_ai_visible = not_met`). 10 checklist calls, about $0.0075. Hunter is still untested live. |

## Phase 6: router findings

| Item | Status | Notes |
|---|---|---|
| `checkpointer=False` subgraphs under a `durability="sync"` parent | **CHANGE** | The Task 0 probe ("compiles, and runs inside a checkpointed parent") ran the parent without `durability="sync"`. The CLI streams with `durability="sync"`, and a subgraph inherits it through its config. With no checkpointer it waits on `loop._put_checkpoint_fut`, which is never set, so it raises `AttributeError` (langgraph 1.2.21). Phases 4 and 5 called the wrappers directly, so their tests missed it; `test_router_prompt` is the first run of the real stages inside the parent graph. Fix: `outreach.graph.invoke_subgraph` passes `durability="exit"` and mutes LangGraph's warning that durability "has no effect" without a checkpointer (it does have an effect here). |
| Per-turn router schema | OK | `decision_schema` builds a Pydantic model per turn with `move: Literal[<legal moves>]` and `question_key: Literal[<available keys>] \| None` (only `None` when `ask` isn't legal). The live API accepted it with `method="json_schema"`. An illegal move fails validation, gets one retry, and then the code router decides. |
| Live router turns | OK | Two Opus turns on sample states, about 1,000 input and 130-160 output tokens each, about $0.007 per turn. Row 4 (2 of 4 qualified, research left): `research`, with the assumption written out. Exhausted with 3 near misses: `ask` with `lower_threshold`, not `extra_round`, because the near misses close the gap at no model cost. |
| Run names | OK | Per-company runs are `research_company:<domain>` and `qualify_lead:<domain>`. The router call is `router`, with `run_id` and `router` in its metadata. |

## Phase 7: end-to-end findings

| Item | Status | Notes |
|---|---|---|
| `exa.ai` in `excludeDomains` | **CHANGE** | Found by the live smoke test. With the phase 4 blocklist (which added `exa.ai`) sent as `excludeDomains`, Exa returned 1-2 results instead of 10 for queries that returned 10 in phase 4. The same list minus `exa.ai` returned 10 again. So every real run since phase 4 would have found almost nothing. `ExaClient` now never sends the entries in `NEVER_SENT_EXCLUDES` (`exa.ai`); `normalize_domain` still drops them locally (`test_exa_client`). |
| Two shapes of `judge_json` | **CHANGE** | The researcher stores `{"judge": {...}, "contact_form_url": ...}`; the drafter and the review screen (phase 3) read `company_name` from the top level, so live drafts and reviews would have named the domain, not the company. Phase 3's canned stages wrote the flat shape, so no test saw it. `RunsDB.judge_facts` is now the one reader; canned stages write the real shape. |
| `owner@` addresses | **CHANGE** | `owner@mailto-only.test` was on no rule's list (not a role word, not the company name, not a person), so a lead whose only address reaches the owner ended as `no_email`. `owner` is now in `role_local_parts`. |
| Fresh install: `outreach leads` | **CHANGE** | Only `run` and `resume` migrated `memory.db`, so `leads` and `purge` on a new install failed with "no such table". Both migrate first now (`test_cli`). |
| Live tests and `--allow-hosts` | **CHANGE** | The conftest's live hook called `pytest_socket.enable_socket()`, but `--allow-hosts` patches `connect()` separately, so live tests still couldn't reach any API. The hook now also lifts the host restriction. No live test had run under pytest before. |
| Fake end to end | OK | `test_end_to_end_fake` drives the real `outreach run` command over the fixture sites with every stage real: CUJ 1 (with a reject), CUJ 5 across two runs, CUJ 4 ("2 of 4 qualified: research rounds used up" after 3 differently worded searches), and CUJ 3 (judge dies on company 3; `run` offers resume; no search, triage or finished fetch is repeated). |
| Live smoke | OK | `tests/live/test_smoke.py`, 2026-11-13: 1 Exa search (10 Tucson roofers), 8 examined, 2 counted, 2 qualified (one through **Hunter, its first live use**), 2 Opus drafts, 2 approved, recorded not mailed. 58 s, $0.031. |

### Cost recheck (design: "under about $1 per run")

Measured per call in the live smoke run (`api_calls`, settings.toml prices):

| Call | Cost |
|---|---|
| Exa search | $0.007 billed here (10 results); $0.022 in the probe (25 results). `exa_dollars_per_call` stays at $0.022 as the cautious figure. |
| Triage (Haiku) | $0.0001 per result |
| Company judge (Haiku) | $0.0015 per company |
| Checklist (Haiku) | $0.0009 per lead |
| Draft (Opus) | $0.009 per draft |
| Router turn (Opus, phase 6) | $0.007 per turn |
| Parse, discovery turn (Haiku) | under $0.0002 each |

- **A typical run** (target 4, one round, about 3 searches, 20 results triaged, 10 companies read, 4 drafts, 4 router turns): about $0.02 + $0.002 + $0.015 + $0.009 + $0.035 + $0.028, so **about $0.11**.
- **The worst case at every cap** (3 rounds x 8 searches at $0.022, 90 results triaged, 30 companies read and scored, 30 drafts, 20 router turns each retried once): about $0.53 + $0.01 + $0.07 + $0.27 + $0.28, so **about $1.15**. Reaching it needs every cap hit at once, plus 30 qualified leads against a target of 4.
- **Verdict:** the expected cost is about a tenth of the design's "under about $1". The worst case is just over it. Hunter credits are separate: at most `max_hunter_calls_per_run` = 5 per run.

### Manual check (2026-11-14 UTC, run `01M7BVMW5BR61EMJQKSE1CRJJ3`, `--router claude`)

- **Sends:** 8 drafts approved, 8 `send_log` rows `sent` through the Mailtrap sandbox, about 11 s apart (`mail_min_send_interval_s` = 10). No other sends. Run status `complete`, stop reason `complete`.
- **Drafts:** Opus wrote specific openers from the site evidence (owners' names, service areas, years in business). All greet "Hi there": none of the 8 had an owner-name address.
- **Still to confirm by eye:** the 8 messages in the Mailtrap inbox, and the LangSmith project grouping this `run_id`.

| Item | Status | Notes |
|---|---|---|
| Intake took a question as the answer | **CHANGE** | "what do you mean" was stored as `sender_name`, and every email was signed with it. Free-text intake replies now go to Haiku first (`prompts/intake_reply.md`, call site `intake_reply`). A question back gets an answer and the same question again, in a cyan "Answer" panel. The call is cached by `llm_cache`, so replaying the node on resume costs nothing, and any failure counts as an answer (`test_intake_reply`). Live: "what do you mean" and "like what? give me an example" were answered with an example; "Sam Lee" and "anthrall" passed as answers. |
| Review screen hid the email | **CHANGE** | The email panel was printed first, then about 12 lines of verdicts, address and keys, so in a short terminal it scrolled away before the prompt. Now the facts come first, then the email, then the keys on one line, right above `review>`. Draft and page text is printed as plain text: as Rich markup, a stray `[/link]` would crash the screen and `[b]` would vanish (`test_screens`). |
| `back` on the first draft | **CHANGE** | Offered even with nothing to go back to. `b` now appears only after a decision, and a stale `back` is refused with a message (`test_resume_review`, `test_screens`). |
| Where the databases are | Noted | The Claude desktop app is a packaged app: processes it starts in its own container see `%LOCALAPPDATA%` redirected to `Packages\Claude_<id>\LocalCache\Local`. The Terminal panel runs outside it, so runs started there use the real `%LOCALAPPDATA%\outreach-bot`. The two copies are different files. |

