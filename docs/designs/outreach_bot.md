# Outreach Bot

*   **Author:** Yomi
*   **Status:** Approved (Stage 9 review accepted at round 2, 2026-11-13: round 1 had 8 Blockers and round 2 had 2, all fixed; the round-2 fixes have not been re-reviewed)
*   **Date:** 2026-11-13

--------------------------------------------------------------------------------

## Overview

Outreach Bot is a command-line program that runs a small, supervised cold-outreach pipeline. You type a request in plain English, for example "find me leads in Tucson that don't use AI". The program then:

1. finds roofing companies in that city on the web;
2. checks each one against your criteria;
3. finds an email address for it that came from a known source;
4. drafts a short email offering two automations:
    * **speed to lead:** replying to new inquiries within minutes;
    * **document reading:** pulling details out of estimates, insurance claims and adjuster reports.

It then pauses so you can approve, edit or reject each draft. Approved drafts go to a test inbox (the Mailtrap sandbox), never to the company.

The program is built with LangGraph and the Anthropic API (Claude). A **supervisor** decides which step runs next. It can run in two modes:

* **Claude router:** Claude picks the next step from the moves that code says are allowed.
* **Code router:** fixed rules pick the next step.

Research and qualification each run as their own **subgraph**. Drafting, review and sending run as a fixed chain the supervisor can't bypass.

The project's main purpose is learning, and to serve as a portfolio piece. It is meant to show four LangGraph skills working in one program:

* pausing for a human and resuming after the terminal closes;
* tool calling;
* tracing every run in LangSmith;
* the supervisor pattern.

v1 is limited to US roofing companies. v2 will accept any industry.

## The Problem

**The problem.** Yomi does no outreach today. Doing it by hand takes time:

* finding local businesses;
* checking whether they fit;
* tracking down a real contact email;
* writing an email for each one.

Off-the-shelf tools (lead databases, sequencers) would do most of this, but they teach nothing about how agent systems are built. The real gap is a working, inspectable example of an agentic LangGraph application that:

* makes routing decisions;
* calls tools;
* stops for a human at the right moments;
* survives crashes and closed terminals without repeating work or sending twice;
* can be traced and explained afterwards.

**Why the obvious versions fail:**

* **One long Claude loop doing everything** costs roughly 10–30 times more, because every fetched page is re-sent on every turn [verify]. It is also hard to resume, and its decisions can't be checked.
* **A Claude supervisor that can route anywhere** can skip the approval step or loop forever.
* **Pausing inside a node that also calls Claude** asks a different question after a resume.
* **Trusting the model to report emails** lets it invent plausible addresses.

**The proposed solution:**

* a supervisor whose legal moves are computed by code;
* human pauses in dedicated nodes that do nothing before pausing;
* research driven by code, with Claude making only judgment calls;
* emails extracted by code from pages the program actually fetched, or from Hunter results that name their source page;
* a send step that checks the approval itself;
* two SQLite files: one for paused runs, which can be wiped, and one for memory across runs, which is never wiped.

## Goals

*   **G1. End-to-end run from one prompt.** "find me leads in Tucson that don't use AI" produces all of the following in one CLI run, with no code edits:
    *   a frozen run plan;
    *   researched companies;
    *   verdicts;
    *   drafts;
    *   your review;
    *   test-inbox sends.
*   **G2. Supervisor with two routers.** `--router=claude` and `--router=code` both complete the same run. Claude is called only when more than one move is legal. Every decision is logged, along with what the code router would have chosen.
*   **G3. Resume without repeating work.** You can stop the run in either of these ways:
    *   close the terminal at any of the three pauses (question, plan confirmation, draft review);
    *   kill the process mid-research.

    Resuming continues from the same point, and no paid call that had already finished is repeated. This covers every cached model call, plus Exa, page fetches and Hunter. An automated test checks it.
*   **G4. No send without approval.** A send happens only when all of these hold:
    *   the draft was approved in this run;
    *   its text matches the approved hash;
    *   it goes to the configured test inbox, on the Mailtrap sandbox host.

    Each approved draft is sent at most once. An automated test has a fake supervisor jump straight to send with no approval, and checks that nothing is sent and that the draft is recorded as `blocked`.
*   **G5. Every email has a source.** Each address on a qualified lead came from one of two places:
    *   extracted by code from a page fetched in this run; or
    *   returned by Hunter Domain Search with at least one source URL.

    A test checks that an address without a source is rejected.
*   **G6. Real tool calling.** Company discovery runs as a Claude tool loop with an `exa_search` tool, through LangGraph's `ToolNode`.
*   **G7. Traceable runs.** Every run appears in LangSmith, tagged with the run ID, round, lead ID and router type. A local record explains each supervisor decision and each lead's outcome.
*   **G8. Bounded runs.** Each run aims for `target_qualified` qualified leads (default 4). The limits:
    *   at most `max_research_rounds` research rounds (default 3; you can approve one extra round mid-run);
    *   at most `max_decisions` router decisions (default 20);
    *   at most `max_questions` mid-run questions (default 3).

    It ends with a recorded stop reason, and an end screen showing the funnel and any choices the supervisor made on its own.
*   **G9. Memory across runs.** A company already contacted, or one you rejected, is never drafted again. Answers you mark "remember this" are not asked again. That holds for both intake and supervisor questions.
*   **G10. Tests without paid calls.** The full graph runs in tests with fake model, search, fetch, Hunter and mail clients.

## Non-Goals

*   **Other industries and countries.** v1 is US roofing only; free-form targets are v2. All roofer-specific values live in one `TargetProfile` file, so v2 is a config and intake change.
*   **Real sending.** No email goes to a real company, and there is no production mail path. If the bot is ever used for real, approved drafts are exported to an established sending tool.
*   **Compliance and deliverability work.** No CAN-SPAM footer, unsubscribe links, or SPF/DKIM/DMARC setup. These become requirements only if real sending is ever considered.
*   **Follow-ups, reply handling, CRM sync, or a web UI.**
*   **Business metrics** such as reply rate, and dollar budgets. Hard per-run limits stay in scope.
*   **Migrating paused runs across app versions.** A version mismatch refuses to resume and explains why.
*   **Concurrency and timeouts.** At most one unfinished run at a time, and a pending question waits without a timeout.
*   **Some tools and features on purpose:**
    *   the LangGraph Store: app tables are the only memory;
    *   the prebuilt `langgraph-supervisor` library: it would hide the logic being demonstrated;
    *   headless browsers and robots.txt handling;
    *   IP pinning against DNS rebinding (see the NFRs);
    *   handling franchises and national chains (v1 skips them at triage).

    The Cloudflare email decoder was moved into v1 on 2026-11-13.

## Open Questions

Questions resolved during drafting and review (2026-11-13) are recorded in the Detailed Implementation Plan under "Decisions". The only item still open:

*   **Vendor facts marked [verify].** Task 0 lists every [verify] item in this document, and has an exit rule.

## Critical User Journeys (CUJs)

### CUJ 1: First run from a prompt

*   **Given**:
    *   API keys are set in `.env`;
    *   `memory.db` is empty;
    *   Mailtrap sandbox credentials are configured;
    *   `--router=claude` (the default) is in use.
*   **When**: Yomi runs `outreach run "find me leads in Tucson that don't use AI"`.
*   **Then**:
    1.  Intake asks for the sender's name, business name and call to action, because none is saved. Yomi can mark the sender profile "remember this".
    2.  Intake shows the plan:
        *   Tucson;
        *   the checklist, including "no AI visible on site", which code checks;
        *   the target of 4;
        *   the editable sender defaults.

        Yomi enters the threshold N and confirms.
    3.  Research shows a progress line for each company.
    4.  The qualifier reports the funnel: how many companies were found, how many met the threshold, and how many have a sourced email.
    5.  Each draft appears on the review screen, one at a time.
    6.  Approved drafts arrive in the Mailtrap sandbox.
    7.  The end screen shows the stop reason, the funnel, and any choices the supervisor made on its own.

### CUJ 2: Close the terminal mid-review and resume

*   **Given**: a run is paused at draft 2 of 4 in review.
*   **When**: Yomi closes the terminal, then later runs `outreach run`.
*   **Then**:
    1.  The program says an unfinished run exists and shows its progress. It offers resume, start new, or discard.
    2.  On resume, draft 2 appears again, with the same text and the same `pause_id`.
    3.  Nothing is sent until review finishes.
    4.  No research, Hunter or drafting call is repeated.

### CUJ 3: Crash during research

*   **Given**: research is on company 6 of a batch.
*   **When**: the process dies, then the run is resumed.
*   **Then**:
    1.  Companies 1–5 keep their results.
    2.  Company 6 is processed again. Every call that had finished before the crash is read from the cache rather than paid for again.
    3.  The batch finishes normally.

### CUJ 4: Not enough leads

*   **Given**: after round 1, only 1 company qualifies.
*   **When**: the supervisor decides what to do next.
*   **Then**:
    1.  It runs research rounds 2 and 3, using different search wording in the same city.
    2.  If research is exhausted with fewer than `target_qualified` leads, the supervisor may ask one question, provided the router is `--router=claude` and questions remain. Which questions are allowed depends on why research ended:
        *   `extra_round` ("Run one more research round in Tucson?"), when the rounds are used up;
        *   `change_city` ("Try a nearby city instead?");
        *   `lower_threshold` ("Lower the checklist threshold?"), when some leads are one point short.

        Each answer changes the run as that question key defines. If no question is asked, the outbox runs for the leads it already has.
    3.  The end screen says, for example, "2 of 4 qualified: research rounds used up".

### CUJ 5: Reject a company

*   **Given**: a draft is on the review screen.
*   **When**: Yomi presses reject.
*   **Then**:
    1.  The screen has already warned that reject blocks the company from all future runs. If the company is fine and only the draft is bad, use edit instead.
    2.  When review ends, the company is added to the do-not-contact list in `memory.db`. Until then, back can undo the reject.
    3.  The company is never drafted again, in this run or any later run.

### CUJ 6: Compare the routers

*   **Given**: the same prompt and the same seeds file.
*   **When**: Yomi runs `outreach run "<prompt>" --router=claude --seeds examples/tucson_demo.txt`, then the same command with `--router=code`.
*   **Then**:
    1.  Both runs complete. The seeds are used in round 1, and any later rounds use discovery.
    2.  For each decision, the decision log shows how it was made (`decided_by`), the legal moves, and the shadow code router's choice.
    3.  LangSmith shows Claude router calls only at turns that had more than one legal move.

## Technical Plan (Architecture)

### Components

```text
+------------------------------------------------------------------------------+
| CLI driver (Typer): run / resume / discard / leads / purge. Writes the runs  |
| row, shows pauses, opens editor, renders progress + end screen, catches      |
| vendor errors and Ctrl+C, flushes tracer on every exit                       |
+--------------------------------------+---------------------------------------+
                                       | stream({run_id}) / Command(resume=...)
                                       v
+------------------------------------------------------------------------------+
| Parent graph (one thread per run; thread_id = run_id)                        |
|                                                                              |
|  START -> parse_request -> fill_from_memory -> intake_missing <-> ask_user   |
|                                                     |             (pause)   |
|                                                     v                        |
|                                              confirm_plan (pause, self-loop) |
|                                                     |                        |
|        +------------------------> SUPERVISOR <------+-----------------+      |
|        |              truth table + Router (claude | code)            |      |
|        |        +-------------+---------+----------+-----------+      |      |
|        |        v             v         v          v           v      |      |
|   run_research  run_qualify  ask_user  drafter   finalize -> END      |      |
|   (wrapper ->   (wrapper ->  (pause)     |                            |      |
|    researcher    qualifier               v                            |      |
|    subgraph)     subgraph)          review_next (pause, loops)        |      |
|        |             |                   |                            |      |
|        +-------------+                   v                            |      |
|           back to supervisor        send_next (guarded, loops) -------+      |
+------------------------------------------------------------------------------+
        |                               |                          |
        v                               v                          v
  runs.db (checkpoints + run      memory.db (registry,       External: Anthropic,
  tables and caches: see Data     do-not-contact, contacted, Exa, websites, Hunter,
  model)                          profiles, checklists,      Mailtrap, LangSmith
                                  preferences)
```

### How a run flows

1.  **Before the graph.** The CLI inserts the `runs` row: `run_id`, `prompt`, `router`, `seeds_json` and `city_override`, with status `active`. It then streams the graph with the input `{run_id}`.
2.  **Intake (a fixed chain, with no supervisor).**
    *   `parse_request` reads the `runs` row. It makes one Haiku structured call and stores the result in `runs.parsed_json`: the city, the target, and the checklist items, each matched to a `code_check` key or left as an `llm` item. If the call returns nothing, the partial plan is empty, and intake asks for every field.
    *   `fill_from_memory` merges in the latest remembered sender profile, checklist and preferences, plus the `TargetProfile` defaults. It writes `run_plans` version 1 with `status='draft'`.
    *   `intake_missing` routes to `ask_user` once for each missing required field: `city`, `sender_name`, `business_name`, `call_to_action`. Each answer returns to `intake_missing`.
    *   `confirm_plan` shows the latest plan version and pauses. Depending on the answer:
        *   `edit` writes the next plan version and loops back to `confirm_plan`;
        *   `confirm` is accepted only once threshold N is valid. It sets the plan to `frozen` and saves the remembered items.
3.  **Supervisor loop.**
    *   Each turn, code builds a `RunStatus` from `runs.db` and computes the legal set from the truth table below.
    *   With one legal move, code takes it (`decided_by=forced`).
    *   With more than one, the selected `Router` decides (`decided_by=router`). If the Claude router fails, the code router decides (`decided_by=fallback`).
    *   The decision is written to `supervisor_decisions` along with the shadow code router's choice. The graph then moves with `Command(goto=ROUTES[move])`.
4.  **Research.** `run_research` opens the next round (or re-enters an open one) in `research_rounds`, and invokes the researcher subgraph. It passes `run_id` and `round` in `config["configurable"]`.
5.  **Qualify.** `run_qualify` invokes the qualifier subgraph for every candidate that has no `qualifications` row.
6.  **Ask.** The supervisor has already stored a `PendingQuestion` with a `QuestionKey`. `ask_user` only shows that stored question and pauses. It then:
    1.  writes the answer to `answers`;
    2.  applies the effect for that key;
    3.  returns control to the supervisor.
7.  **Outbox (a fixed chain).**
    1.  `drafter` writes one draft for each qualified lead that has no draft, isn't `draft_failed`, and whose domain isn't on the do-not-contact or contacted list.
    2.  `review_next` shows the draft with the lowest `queue_pos` that has no final decision, and pauses. The possible answers:
        *   approve;
        *   edit: you edit the draft in your text editor, and saving counts as approval;
        *   reject;
        *   skip: moves the draft to the end of the queue;
        *   back: reopens the most recent final decision that hasn't been superseded. Skips aren't decisions, so back passes over them. On the first draft, back shows the same draft again.

        Once every draft has a final decision (approve, edit or reject), `review_next` writes all reject blocks to `do_not_contact` and hands over to `send_next`.
    3.  `send_next` takes the next draft in the run that has `status=ok`, has no `send_log` row, and whose final review isn't a reject. It applies the send guard, then loops.
    4.  When no such draft is left, control returns to the supervisor.
8.  **Finalize.** `finalize` chooses the stop reason by the rule in the Detailed Implementation Plan. It is the only node that writes the final `runs.status`. It releases unfinished registry claims and builds the end-screen data. The CLI renders the end screen and calls `tracing.flush()`.

### Supervisor contract

*   **Input, `RunStatus`.** Every field is computed from `runs.db` on each turn; none is stored in graph state. It holds no page text and no model text.

    | Field | Type | Source |
    |---|---|---|
    | `unqualified_candidates` | int | `run_candidates` rows in status `candidate` or `parked_no_email` that have no `qualifications` row |
    | `qualified_count` | int | `qualifications` rows with `qualified = 1` |
    | `near_miss_leads` | int | `qualifications` rows with `points = N − 1` |
    | `target_qualified` | int | frozen plan |
    | `research_rounds_used` | int | `COUNT(*)` in `research_rounds` for this run |
    | `max_research_rounds` | int | settings + `run_overrides` extra rounds |
    | `last_round_new_companies` | int or None | `new_companies` of the latest closed round of the latest plan version; None if there isn't one |
    | `undrafted_qualified` | int | qualified leads with no draft, not `draft_failed`, and not on the do-not-contact or contacted lists |
    | `questions_used`, `max_questions` | int | `answers WHERE origin='supervisor'`; settings |
    | `decisions_used`, `max_decisions` | int | `supervisor_decisions WHERE decided_by IN ('router','fallback')`; settings |
    | `answered_keys` | set of `QuestionKey` | `answers` |
    | `available_question_keys` | set of `QuestionKey` | each supervisor key whose "Asked when" condition holds and which isn't in `answered_keys` |
    | `exhausted_reason` | `"rounds"`, `"empty_round"` or None | see below |
    | `last_stage_stop_reason` | `StageStopReason` or None | `research_rounds.stop_reason` of the latest round, or the qualifier's last result row |

*   **Derived values:**
    *   `complete`: `qualified_count >= target_qualified`.
    *   `exhausted_reason`:
        *   `"rounds"` if `research_rounds_used >= max_research_rounds`;
        *   otherwise `"empty_round"` if `last_round_new_companies == 0`;
        *   otherwise None.
    *   `exhausted`: `exhausted_reason` is not None.
    *   `ask_available`: the router is `claude`, `questions_used < max_questions`, and `available_question_keys` is not empty.
*   **Moves:** `research`, `qualify`, `ask`, `draft` and `finish`. Sending isn't a move; it can only be reached through the outbox.
*   **Truth table.** Rows are checked top to bottom, and the first match wins. The legal set is never empty, because rows 2, 3, 5 and 6 always contain `draft` or `finish`.

    | # | Condition | Legal set |
    |---|---|---|
    | 1 | `unqualified_candidates > 0` | {`qualify`} |
    | 2 | `decisions_used >= max_decisions` | {`draft`} if `undrafted_qualified > 0`, else {`finish`} |
    | 3 | `complete` | {`draft`} if `undrafted_qualified > 0`, else {`finish`} |
    | 4 | not `exhausted` | {`research`}, plus `draft` if `undrafted_qualified > 0` |
    | 5 | `exhausted` and `ask_available` | {`ask`}, plus `draft` if `undrafted_qualified > 0`, else `finish` |
    | 6 | otherwise | {`draft`} if `undrafted_qualified > 0`, else {`finish`} |

    The outbox always drains before it returns to the supervisor, so review and send never leave work behind for the supervisor to see.

*   **Remembered supervisor answers.** If a key in `available_question_keys` has a remembered answer in `preferences`, the supervisor applies it straight away instead of asking. It writes it to `answers` with `origin='memory'`, and records "applied remembered answer for <key>" as an assumption. These don't count toward `questions_used`.
*   **Output, `Decision`:**
    *   `move: Move`
    *   `reason: str` (cut to 200 characters in code)
    *   `question_key: QuestionKey | None` (the type is `None` when `ask` isn't legal)
    *   `assumption: str | None`

    A `move=ask` with no key is treated as a failure, and the code router decides.
*   **Code router rule order** (matching the table):
    1.  take `qualify` if it is legal;
    2.  otherwise, at row 4, take `research`;
    3.  otherwise, take `draft` if it is legal;
    4.  otherwise, take `finish`.

    The code router never picks `ask`.
*   **Retries.** The `llm.py` helper owns the single validation retry. If the Claude router still fails, or returns a move outside the legal set, the code router decides that turn. A router turn makes at most 2 model calls. Turns decided by `router` or `fallback` count toward `max_decisions`, under either router.

### Questions the supervisor can ask

| `QuestionKey` | Asked when | Allowed answers | Effect |
|---|---|---|---|
| `extra_round` | `exhausted_reason == "rounds"`, target not met | yes / no | yes: `run_overrides(extra_round, +1)`, so `max_research_rounds` goes up by 1 |
| `change_city` | `exhausted`, target not met | a city name / no | a city: write a new `run_plans` version for that city, plus one `run_overrides` extra round. `last_round_new_companies` resets because it reads the latest plan version |
| `lower_threshold` | `exhausted`, target not met, `near_miss_leads > 0` | an integer from 1 to N−1 / no | write a new checklist version with the lower N. `rescore` recomputes `qualified` with no model call, and deletes the `qualifications` row of every newly passing lead that has no on-site email. Those leads go back through `qualify`: their verdicts come from `llm_cache`, and Hunter runs for them |

*   Each key can be asked at most once per run.
*   The question text comes from a fixed template in `TargetProfile.question_templates`, never from model output.
*   Intake has its own keys (`city`, `sender_name`, `business_name`, `call_to_action`). They don't count toward `max_questions`.

### Pause contract

*   **Payload.** `PausePayload(kind, pause_id, prompt_text, allowed_answers, context)`, where `kind` is `question`, `plan` or `draft_review`. It is passed to `interrupt()` as `model_dump(mode="json")`.
*   **`pause_id`** is computed from database state, never generated, so a resumed node produces the same id:
    *   plan: `plan:<run_id>:<plan_version>`
    *   question: `q:<run_id>:<key>`
    *   review: `review:<draft_id>:<review_seq>`
*   **Answers.** `Answer` is a tagged union. Each class has a `kind` Literal as its discriminator:
    *   `PlanAnswer(kind="plan", pause_id, action: confirm|edit, edits: dict[str, str], remember: set[Literal["sender","checklist"]])`
    *   `QuestionAnswer(kind="question", pause_id, key, value: str, remember: bool)`
    *   `ReviewAnswer(kind="draft_review", pause_id, draft_id, shown_hash, action: approve|edit|reject|skip|back, subject: str|None, body: str|None)`

    The CLI resumes with `Command(resume=answer.model_dump(mode="json"))`, and the node validates it with `TypeAdapter(Answer)`.
*   **`PlanAnswer.edits` keys:**
    *   `city`, `target`, `threshold`;
    *   `sender.name`, `sender.business_name`, `sender.call_to_action`, `sender.offer_text`, `sender.tone`, `sender.max_words`, `sender.signature`;
    *   `item.add` (the value is the item text);
    *   `item.<id>.text`;
    *   `item.<id>.remove`.

    `confirm` ignores `edits`.
*   **Mismatch.** If the `pause_id` or `shown_hash` doesn't match, the node calls `interrupt()` again with the same payload, and the CLI shows "answer did not match the current question; showing it again".
*   **Rule.** Nothing may happen before `interrupt()` except deterministic database reads: no model call, no database write, no other side effect.
*   **Finding the pause.** After each `stream()`, the CLI reads pending interrupts from `graph.get_state(config).tasks[*].interrupts` [verify].
*   **Editor.** The text editor opens in the CLI, never inside a node.

### Subgraph contracts

*   **Attachment.**
    *   Each subgraph is compiled once, with `checkpointer=False`, so it doesn't inherit the parent's checkpointer [verify]. It is invoked inside its wrapper node.
    *   If the process dies, the wrapper runs again from its first line on resume. Progress is rebuilt from `runs.db`, and every paid call reads through a cache, so the re-run pays nothing again.
    *   The wrapper passes `run_id`, `round` and child metadata in `config["configurable"]` and `config["metadata"]`, with `run_name` set.
*   **Researcher.** Private state:
    *   `discovery_messages: Annotated[list[AnyMessage], add_messages]`;
    *   `cursor`.

    It runs these steps:

    *   **Round 1 with seeds.** The seeds go through `memory_db.claim()`, which applies `should_skip`, and are queued directly. Discovery and triage are skipped. Seeds are used in round 1 only.
    *   **Discovery** (every other round). A Haiku tool loop through `ToolNode([exa_search], messages_key="discovery_messages")` [verify].
        *   It is skipped when the round's queue rows already exist.
        *   The tool reads `run_id` and `round` from its injected `RunnableConfig` [verify]. It removes blocklisted, shared-host and `should_skip` domains, then queues new domains with `INSERT OR IGNORE`. Domains already queued in this run are not new.
        *   Each returned row is at most `snippet_chars` characters.
        *   The loop stops when the model ends its turn, when `max_exa_searches_per_round` is reached, or when `max_companies_examined_per_round` domains are queued.
    *   **Triage.** `classify_site` labels each queued domain from its Exa title and snippet. Only `contractor_own_site` goes on. Every other label sets `rejected_triage`, with the label stored in `drop_reason`.
    *   **Per company**, one per step, in code:
        1.  claim the company in the registry;
        2.  fetch the home page, plus up to `extra_pages_per_company` linked pages;
        3.  extract emails, phone numbers, forms and AI-widget markers from the raw HTML;
        4.  make one Haiku judgment call (own site? company name, people and roles, stated locations, each with a quote);
        5.  code checks every quote against the stored page text and decides on the US evidence;
        6.  write the evidence, then finalize the registry row.
    *   **Stops at** `batch_size` counted companies, any research limit, or an empty queue. Queued rows left over are carried into the next round first.
*   **Qualifier.** Its private state is a cursor. For each lead it runs these steps:
    1.  score the checklist;
    2.  if the lead has no on-site email and its points are at least N, call Hunter Domain Search, within the cap;
    3.  choose the email;
    4.  always write a `qualifications` row.
*   Neither subgraph pauses.

### Send guard

`send_next` selects the next draft in the run that has `status=ok`, has no `send_log` row, and whose final review is not a reject. It sends only when all of these checks pass, in this order:

1.  An approved review for this draft exists in this run and has not been superseded.
2.  `sha256(subject + "\n\n" + body)` equals that review's `approved_hash`.
3.  The lead's domain is not in `memory.contacted` or `memory.do_not_contact`.
4.  The recipient equals `mail_test_recipient`.
5.  `mail_smtp_host` is in `mail_host_allowlist`.

**When a check fails,** `send_next` writes `send_log(status='blocked', reason=<check name>)`, so the draft leaves the queue.

**When all checks pass:**

1.  `send_begin` writes `sending` and calls `mark_contacted`.
2.  The draft is sent over SMTP.
3.  `send_complete` writes `sent` with the message ID. On an SMTP error it writes `failed` with the error text, and v1 doesn't retry.

**On resume,** the CLI calls `mark_sending_unknown`, which turns any `sending` row into `unknown`. Unknown drafts are never retried.

The guard doesn't depend on how `send_next` was reached.

### Storage

*   `runs.db` (can be wiped) holds the LangGraph checkpoints and the run tables.
*   `memory.db` (kept, versioned, backed up before migrations) holds the cross-run tables.

The Data model in the Detailed Implementation Plan lists every table and its columns. Graph state holds only the fields of `ParentState`, and stores them as JSON-compatible dicts.

### Model use

| Call | Model | When | Output |
|---|---|---|---|
| `parse_request` | `claude-haiku-5-5` | once per run | partial plan |
| Discovery tool loop | `claude-haiku-5-5` | each research round without seeds | `exa_search` calls |
| Triage (`classify_site`) | `claude-haiku-5-5` | once per new domain | label and confidence |
| Company judgment | `claude-haiku-5-5` | once per company | own-site flag, name, people and locations, with quotes |
| Checklist judgment | `claude-haiku-5-5` | once per candidate with `llm` items | verdicts with quotes |
| Claude router | `claude-opus-5-5` | only when more than one move is legal | `Decision` |
| Drafter | `claude-opus-5-5` | once per qualified lead | subject and body |

Prices: Haiku 5.5 costs $0.10 / $0.50 per million input / output tokens, and Opus 5.5 costs $4 / $20. On Opus 5.5, forced `tool_choice` returns an error and thinking can't be turned off. Its default effort is `medium`, so effort is set explicitly for every call site. (Source: Claude API reference, 2026-11-03.)

Structured output uses the `json_schema` method [verify in Task 0]. Each call site gets its own `ChatAnthropic` instance with its own effort, `max_tokens` and `max_retries=2`.

### Observability

*   Every run uses `thread_id = run_id`.
*   Model calls and sub-invokes carry `run_id`, `round`, `lead_id` and `router` in `config["metadata"]` [verify]. Per-company runs are named `research_company:<domain>` through `run_name`.
*   LangSmith tool spans log the page ID, URL and status, never page text.
*   `supervisor_decisions` and `api_calls` keep a local record that doesn't expire.
*   **End screen.** It shows:
    *   the funnel: found, counted, qualified, drafted, approved, sent, blocked, failed;
    *   drop reasons;
    *   searches, pages, tokens and Hunter credits used;
    *   assumptions the supervisor made on its own;
    *   "`qualified_count` of `target_qualified`", with the stop reason.

## Alternatives Considered

### Alternative A: Fixed pipeline with no supervisor

*   **Description**: Six stages in a straight line (setup, research, qualify, draft, approval, send), with fixed edges and conditional edges for loops.
*   **Pros**:
    *   simplest graph;
    *   fully predictable;
    *   no routing cost;
    *   easiest to test.
*   **Cons**:
    *   doesn't demonstrate the supervisor pattern, which is a learning goal;
    *   "research again or draft what we have" becomes a hard-coded threshold.
*   **Reason for Rejection**: Rejected by author: the supervisor pattern is part of what the project must show. The fixed-pipeline behaviour stays available as `--router=code`.

### Alternative B: Claude makes every routing decision

*   **Description**: The supervisor calls Claude on every hop and may choose any route.
*   **Pros**:
    *   every hop is visible as a model decision;
    *   minimal routing code.
*   **Cons**:
    *   most hops have only one legal move, so calling Claude adds cost and run-to-run variation without adding judgment;
    *   tests depend on a fake model;
    *   an unconstrained choice can skip stages or loop.
*   **Reason for Rejection**: Rejected by author: Claude decides only when more than one move is legal, and only among the moves code allows.

### Alternative C: Code router only

*   **Description**: Fixed rules decide every route.
*   **Pros**:
    *   deterministic;
    *   free;
    *   easy to test.
*   **Cons**:
    *   no agentic routing to demonstrate;
    *   nothing to compare against.
*   **Reason for Rejection**: Rejected by author as the only mode. It is kept as `--router=code`, the test default, and the shadow router.

### Alternative D: The prebuilt `langgraph-supervisor` library

*   **Pros**:
    *   less code;
    *   a documented pattern.
*   **Cons**:
    *   hides the routing logic, the legal-move guards and the pause handling;
    *   assumes agents hand off through the shared message list [verify].
*   **Reason for Rejection**: Rejected by author, following architecture review finding 2.5.

### Alternative E: One Claude tool loop for all research

*   **Pros**:
    *   the most "agentic";
    *   the least orchestration code.
*   **Cons**:
    *   every fetched page is re-sent on every turn, about $6–30 per batch against $0.35–1 for the hybrid [verify];
    *   checkpoints grow with every page;
    *   a crash restarts the whole conversation;
    *   Claude, not code, reports the emails.
*   **Reason for Rejection**: Rejected by author (section 1 review, A1).

### Alternative F: Claude's built-in web search tool

*   **Pros**:
    *   one API key;
    *   no fetcher code.
*   **Cons**:
    *   the searches run inside one API call, so LangGraph sees no tool calls;
    *   page content comes back encrypted, so code can't check where an email came from [verify];
    *   a crash re-pays for the whole step.
*   **Reason for Rejection**: Rejected by author (round 1).

### Alternative G: Raw Anthropic SDK with a custom tool node

*   **Pros**:
    *   one fewer dependency layer;
    *   new Anthropic content blocks arrive sooner.
*   **Cons**:
    *   not the standard LangGraph tool-calling pattern;
    *   needs manual LangSmith wrapping.
*   **Reason for Rejection**: Rejected by author (section 1 review, A2), in favour of `ChatAnthropic` with `ToolNode`.

### Alternative H: Each lead runs through every stage on its own

*   **Pros**:
    *   the first draft is ready sooner;
    *   one slow lead doesn't block the others.
*   **Cons**:
    *   several review pauses can be waiting at once;
    *   rate limits;
    *   harder to resume.
*   **Reason for Rejection**: Rejected by author: research returns a batch, and drafts are reviewed one at a time.

### Alternative I: Hunter Email Finder

*   **Pros**: a higher hit rate.
*   **Cons**: it predicts addresses from name patterns.
*   **Reason for Rejection**: Rejected by author: it breaks the rule that addresses are never guessed.

### Alternative J: LangGraph Store for memory across runs

*   **Pros**: demonstrates another LangGraph feature.
*   **Cons**: memory would live in two places.
*   **Reason for Rejection**: Rejected by author (architecture review, 5.1).

### Alternative K: One SQLite file for everything

*   **Pros**: simplest setup.
*   **Cons**: wiping it would erase the do-not-contact list.
*   **Reason for Rejection**: Rejected by author: two files.

### Alternative L: No questions mid-run

*   **Pros**:
    *   a run never waits for you after plan confirmation;
    *   a simpler router.
*   **Cons**: the supervisor has to guess on choices that spend more or that change who could be contacted.
*   **Reason for Rejection**: Rejected by author: three fixed question keys are allowed, with a cap of 3.

### Alternative M: A real sending path

*   **Pros**: actual outreach.
*   **Cons**:
    *   reputation and legal risk;
    *   bounces;
    *   not needed for learning LangGraph.
*   **Reason for Rejection**: Rejected by author: test inbox only.

### Alternative N: TypeSafe Jev for yes/no judgments

*   **Pros**:
    *   possibly cheaper label decisions;
    *   a second model to compare against.
*   **Cons**:
    *   no text output, so it can't produce quotes;
    *   a second vendor;
    *   saves only cents.
*   **Reason for Rejection**: Deferred by author. Triage sits behind the `classify_site` interface (`research/triage.py`), so Jev can be compared later.

### Alternative O: Pin each fetch connection to the checked IP

*   **Description**: A custom httpx transport resolves DNS once, checks the address, and connects to that IP while keeping the TLS hostname.
*   **Pros**: closes the DNS-rebinding gap between the IP check and the connection.
*   **Cons**:
    *   a custom networking layer, and its TLS handling has to be right;
    *   it isn't needed for a local CLI that fetches domains taken from search results.
*   **Reason for Rejection**: Rejected by author (Stage 9 round 2). Every hop is still resolved and checked. The remaining risk is stated in the NFRs.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Safety of sending.**
    *   The send guard is the main safety boundary.
    *   The mailer refuses to start unless `mail_smtp_host` is on the allowlist.
    *   It refuses any recipient other than `mail_test_recipient`.
    *   The lead's real address appears only in the body and in `X-Intended-Recipient`.
*   **Prompt injection.**
    *   Web text reaches four calls:
        *   discovery, through Exa titles and snippets;
        *   triage, through Exa titles and snippets;
        *   company judgment, through cleaned page text inside `<page_content>` delimiters;
        *   checklist judgment, the same way.
    *   Only discovery has a tool, and code filters its results.
    *   Every judgment output is checked by code.
    *   The router never sees web text.
    *   Question text comes from fixed templates.
*   **The fetcher** is not a model tool. It builds URLs only from a claimed domain and a path. On every redirect hop it resolves DNS and refuses private, loopback and link-local addresses. **Accepted risk:** a host could change its DNS answer between that check and the connection (DNS rebinding). This is accepted for a local CLI that fetches domains taken from search results (Alternative O).
*   **Personal data.**
    *   Names and emails of real business owners go to Anthropic, Hunter and LangSmith, and are stored in local SQLite.
    *   Tool spans don't contain page text.
    *   `.env`, `*.db` and backups are git-ignored.
    *   Fixtures use made-up people on `*.test` domains.
    *   `outreach purge <domain>` deletes a company from both databases. `outreach purge <city>` does the same for every company whose `leads.city` matches, or that a run targeting that city found; it lists them and asks first (`--yes` skips the question).
*   **Cost.** Expected cost is under about $1 per run. Phase 7 recheck: about $0.11 for a typical run and about $1.15 with every cap hit at once (`docs/vendor_notes.md`). The hard caps bound the worst case, and `api_calls` records the usage.
*   **Rollback and versioning.**
    *   `runs.db` can be wiped when its schema changes.
    *   `memory.db` uses numbered migrations, with a backup first.
    *   A version mismatch refuses `resume` (exit 4), but still allows `discard` and starting a new run.

### Additional Resources

*   [concepts_brief.md](../../egm/concepts_brief.md) is the current brief, and [concepts_brief_history.md](../../egm/concepts_brief_history.md) is the round-by-round record.
*   Goldfish reviews:
    *   [goldfish_review_round1.md](../../egm/goldfish_review_round1.md)
    *   [goldfish_review_section1.md](../../egm/goldfish_review_section1.md)
    *   [goldfish_review_architecture.md](../../egm/goldfish_review_architecture.md)
*   Stage 9 review:
    *   round 1: [implementation_readiness_report_r1.md](../../egm/implementation_readiness_report_r1.md), resolved in [goldfish_review_stage9_r1.md](../../egm/goldfish_review_stage9_r1.md);
    *   round 2: [implementation_readiness_report_r2.md](../../egm/implementation_readiness_report_r2.md), resolved in [goldfish_review_stage9_r2.md](../../egm/goldfish_review_stage9_r2.md).
*   [docs/architecture_supervisor_sketch.png](../architecture_supervisor_sketch.png) is Yomi's architecture sketch.

### Future Improvements

*   **v2 free-form targets.** Generate a `TargetProfile` from the prompt.
*   **Export instead of send.** Write approved drafts and their sources to CSV.
*   **Jev comparison.** Swap Jev in behind `classify_site`.
*   **Router disagreement report.** Summarise `supervisor_decisions` across runs.
*   **Extra page handling.** A headless browser, robots.txt handling, a franchise policy, IP pinning.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Decisions

| Topic | Decision |
|---|---|
| Checklist scoring | Each item scores 1 if "met" and 0 if "not met" or "unknown". A lead qualifies when its points are at least N **and** it has an email with a source. |
| Threshold N | Collected only at `confirm_plan`, which won't accept `confirm` until N is valid: an integer from 1 to the number of items, checked again after every item edit. The authoritative copy is `threshold` inside `checklist_json` in the frozen `run_plans` row. `memory.checklists.threshold` is only the remembered default. |
| Checklist reuse | If a remembered checklist exists, it is the starting point. Otherwise the checklist is built from the prompt and the `TargetProfile` defaults. |
| Sender profile | Intake asks for the name, business name and call to action. The offer text, tone, length limit and signature come from `TargetProfile`, and can be edited at `confirm_plan`. |
| Models | See Model use. |
| Edited drafts | Saving counts as approval, and the hash is taken from the saved text. An empty or unchanged file means "cancelled". |
| Review actions | See How a run flows, step 7. |
| Qualified-list pause | None. |
| Owner name match | The part before the `@`, lowercased with accents and hyphens removed, must exactly equal `first`, `last`, `first.last`, `firstlast`, `first_last`, `flast` or `firstl`, built from the name of a person whose role or quote contains an ownership word. |
| Hunter | Called only for a lead with no on-site email whose points are at least N. Uses Domain Search only, keeps only results with a source URL, and caches them by domain. Every lead gets a `qualifications` row, including leads skipped because of the cap. |
| Discovery | Directories are skipped via the blocklist and `classify_site`. Domains are keyed with `tldextract`. Shared site-builder hosts, Facebook-only businesses and franchise hubs are skipped. A company needs US evidence. |
| Site reading | The home page, plus up to `extra_pages_per_company` pages chosen by keyword. The fetcher takes paths only. Emails are extracted by code, including Cloudflare decoding. `--seeds` is used in round 1 only and skips triage. |
| Mid-run questions | See "Questions the supervisor can ask". |
| DNS rebinding | Accepted risk, with no IP pinning (Alternative O). |

### Data model

#### Enums (`src/outreach/models.py`)

| Enum | Values |
|---|---|
| `Move` | `research`, `qualify`, `ask`, `draft`, `finish` |
| `QuestionKey` | supervisor: `extra_round`, `change_city`, `lower_threshold`; intake: `city`, `sender_name`, `business_name`, `call_to_action` |
| `StopReason` (run, with end-screen text) | `complete` "target reached"; `exhausted_rounds` "research rounds used up"; `exhausted_empty_round` "a research round found no new companies"; `decision_cap` "supervisor decision limit reached"; `no_candidates` "no usable companies found"; `user_discarded` "discarded" |
| `StageStopReason` | `filled`, `queue_empty`, `limit_searches`, `limit_companies`, `seeds_done`, `limit_hunter` (for information only) |
| `RunStatusValue` (`runs.status`) | `active`, `paused`, `failed`, `complete`, `discarded`. **Unfinished** means `active`, `paused` or `failed`. |
| `ResearchStatus` | `queued`, `researching`, `candidate`, `parked_no_email`, `rejected_triage`, `rejected_not_roofer`, `rejected_non_us`, `rejected_fetch_failed`, `released` |
| `DropReason` | `below_threshold`, `no_email`, `hunter_cap`, `hunter_quota`, `triage:<label>`, `not_roofer`, `non_us`, `fetch_failed` |
| `FetchStatus` | `ok`, `blocked`, `js_shell`, `timeout`, `http_error`, `offsite_redirect`, `soft_404`, `private_ip` |
| `DomainRelation` | `same_domain`, `freemail`, `other_domain` |
| `ExtractMethod` | `visible_text`, `mailto`, `jsonld`, `cf_decoded`, `text_obfuscated`, `hunter` |
| `VerdictValue` | `met`, `not_met`, `unknown` |
| `EmailType` | `owner`, `general` |
| `TriageLabel` | `contractor_own_site`, `directory`, `listicle`, `lead_gen`, `manufacturer_supplier`, `franchise_hub`, `other` |
| `ReviewAction` | `approve`, `edit`, `reject`, `skip`, `back` |
| `SendStatus` | `sending`, `sent`, `failed`, `blocked`, `unknown` |
| `DecidedBy` | `forced` (one legal move), `router` (the selected router chose), `fallback` (the code router took over after a Claude failure) |

**`ResearchStatus` transitions:**

*   `queued` → `researching` or `rejected_triage`
*   `researching` → `candidate`, `parked_no_email`, `rejected_not_roofer`, `rejected_non_us` or `rejected_fetch_failed`
*   `queued` or `researching` → `released`, when `finalize` or `discard` runs while rows are still `queued` or `researching`

**Terms:**

*   **Parked** means `parked_no_email`.
*   **`lead_id`** is the normalized `domain_key` string, the same value in both database files. `lead_id` is the name used in run tables, and `domain_key` the name used in registry tables.

#### `runs.db` tables (`src/outreach/db/schema_runs.sql`)

| Table | Columns (PK **bold**, U = unique) |
|---|---|
| `runs` | **run_id** TEXT (ULID), created_at, app_version, status, stop_reason, router, prompt, seeds_json, city_override, parsed_json, plan_version INT |
| `run_plans` | **(run_id, plan_version)**, status (`draft`/`frozen`), city, target_qualified, checklist_json (holds items and threshold), checklist_key, sender_json |
| `run_overrides` | **(run_id, key, seq)**, value |
| `answers` | **pause_id**, run_id, key, value, origin (`intake`/`supervisor`/`memory`), answered_at |
| `research_rounds` | **(run_id, round)**, plan_version, started_at, closed_at, new_companies INT, stop_reason |
| `search_calls` | **request_hash** (sha256 of run_id, round, query, canonical args), run_id, round, query, response_json, created_at |
| `run_candidates` | **(run_id, lead_id)**, round, queue_pos INT, status, counted INT, found_by_query, judge_json, us_evidence, drop_reason |
| `triage_labels` | **domain_key**, label, confidence REAL, reason, prompt_version |
| `pages` | **page_id** INTEGER, run_id, lead_id, requested_url, final_url, http_status, fetch_status, content_type, raw_html_zlib BLOB, clean_text, sha256, markers_json, fetched_at; U(run_id, requested_url) |
| `evidence` | **evidence_id**, run_id, lead_id, page_id, kind (`name`/`person`/`location`/`verdict`/`own_site`), quote |
| `email_observations` | **obs_id**, run_id, lead_id, address_norm, address_as_seen, page_id NULL, source_url, method, domain_relation, owner_link (`none`/`name_match`), matched_form NULL, hunter_json NULL; U(run_id, lead_id, address_norm, method) |
| `phones_seen` | **(run_id, lead_id, e164)**, page_id |
| `qualifications` | **(run_id, lead_id)**, checklist_version, points INT, verdicts_json, qualified INT, email, email_type, email_source_kind (`page`/`hunter`), email_source_url, greeting_name, drop_reason |
| `llm_cache` | **key** (sha256 of call site, model, prompt_version and input), response_json, created_at |
| `hunter_cache` | **domain_key**, response_json, fetched_at |
| `drafts` | **draft_id**, run_id, lead_id, subject, body, text_hash, claim_flags_json, model, prompt_version, status (`ok`/`draft_failed`), queue_pos INT; U(run_id, lead_id) |
| `reviews` | **(draft_id, review_seq)**, action, final_subject, final_body, approved_hash NULL, superseded INT, decided_at |
| `send_log` | **draft_id**, run_id, domain_key, status, reason, message_id, started_at, finished_at |
| `supervisor_decisions` | **(run_id, turn)** (turn = 1 + count of earlier rows), status_json, legal_moves, router, decided_by, chosen_move, shadow_move, reason, question_key, assumption |
| `api_calls` | **call_id**, run_id, vendor, call_site, model, input_tokens, output_tokens, cached_tokens, units, stop_reason, cost_estimate, created_at |

#### `memory.db` tables (`src/outreach/db/memory_schema/001_init.sql`)

| Table | Columns |
|---|---|
| `leads` | **domain_key**, research_status, first_run_id, last_run_id, last_checklist_key, normalization_version, updated_at, city (v2: the run plan city the lead was first found in; backfilled from runs.db) |
| `do_not_contact` | **domain_key**, reason, run_id, created_at |
| `contacted` | **domain_key**, run_id, draft_id, sent_at |
| `sender_profiles` | **profile_id**, name, business_name, call_to_action, remembered INT, created_at |
| `checklists` | **(checklist_key)**, items_json, threshold, remembered INT, created_at |
| `preferences` | **key**, value, remembered INT, updated_at |

Only `leads`, `do_not_contact` and `contacted` are keyed by domain. A `checklist_key` is the sha256 of the normalized items (sorted text and check type) plus the threshold.

#### Pydantic models (`src/outreach/models.py`)

*   **`ChecklistItem(id, text, check: Literal["code","llm"], code_check: str|None)`** and **`Checklist(key, version: int, items, threshold: int|None)`**.
*   **`SenderProfile(name, business_name, call_to_action, offer_text, tone, max_words: int, signature)`**.
*   **`RunPlan(run_id, version, status, city, target_qualified, checklist: Checklist, sender: SenderProfile)`**. The router and seeds live on `runs`.
*   **`RunStatus`**: the fields in the Supervisor contract table.
*   **`Decision`**: see the Supervisor contract.
*   **`PendingQuestion(key, origin: Literal["intake","supervisor"], prompt_text, allowed_answers: list[str]|None)`**. `prompt_text` is rendered from `TargetProfile.question_templates[key]`.
*   **`PausePayload`** and the **`Answer`** variants: see the Pause contract.
*   **`Person(name, role, quote, page_id)`**, **`Location(text, quote, page_id)`** and **`JudgeResult(is_single_roofer_own_site: bool, own_site_quote, company_name, name_quote, people: list[Person], locations: list[Location])`**.
*   **`CandidateV1`**: `schema_version: Literal[1]`, `lead_id`, `run_id`, `round`, `domain_key`, `site_url`, `company_name`, `people`, `locations`, `us_evidence`, `email_observations`, `phones`, `contact_form_url`, `page_ids`, `research_status`.
*   **`EmailObservation`** mirrors its table.
*   **`Verdict(item_id, value, quote, page_id)`**.
*   **`QualifiedLead`** mirrors `qualifications`.
*   **`Draft(draft_id, lead_id, subject, body, text_hash, claim_flags, status)`**.
*   **`StageResult(stage, round, counts, stop_reason, new_ids)`**.
*   **`ProgressEvent(kind, run_id, round, lead_id, status, counts)`**.
*   **`ParentState`** (TypedDict): `run_id: str`, `pending_question: dict|None` (a dumped `PendingQuestion`), `stop_reason: str|None`. Everything else is computed from `runs.db`.

### Rules

*   **`should_skip(domain_key, run_id)`** (`memory_db.py`) skips a domain when it is on `do_not_contact`, or in `contacted`, or has been `rejected_*` within `rejected_retry_days`, or is `candidate` or `parked_no_email` with `last_checklist_key` equal to this run's `checklist_key`. Otherwise the domain is researched.
    *   **Callers:** the `exa_search` tool, and `memory_db.claim()`. `claim()` refuses skipped domains, and seeds also go through `claim()`.
    *   **The drafter** separately excludes domains on `do_not_contact` and `contacted`.
*   **Write order across the two files.** `runs.db` evidence rows are written first, and the `memory.db` registry is finalized last. A crash in between leaves the row `researching`, so it is processed again from the caches. Only `memory_db.py` writes to the registry. `decide` calls `memory_db.record_qualification(domain_key, checklist_key)`.
*   **Counting.** A company counts toward `batch_size` when it ends as `candidate` or `parked_no_email`.
*   **Email categories.**
    *   **General address:** the local part is in `role_local_parts`, or it contains the company's own name (the company name with words like "Roofing" and "LLC" removed, at least 4 letters, for example `ridgelineaz@gmail.com` for "Ridgeline LLC") and isn't a staff personal address. Added in phase 5 after the live run: some small roofers' only address is a freemail named after the business.
    *   **Staff personal address:** not general, not an owner name match, and either shaped like a name or matching a non-owner person. These are never chosen.
*   **Choosing an email.** Only `same_domain` and `freemail` addresses can be chosen. An `other_domain` address doesn't count as on-site for the Hunter trigger. Preference order:
    1.  an owner name match, which gives `email_type=owner`;
    2.  a Hunter address with `seniority == "executive"`, or a `position` matching `/owner|founder|president|ceo/i` (field names verified in Task 0). This is `owner` only if it also name-matches an owner, otherwise `general`;
    3.  a general address: on-site and on the role list, or Hunter `type == "generic"`.
*   **Greeting name.** The owner's first name for `email_type=owner` (unless `matched_form == "first"` and no ownership quote exists). Otherwise "Hi there".
*   **US evidence.** Either of these:
    *   a US state name or code in a JSON-LD or page address;
    *   a phone number that passes `phonenumbers.is_valid_number(n)` with `region_code_for_number(n) == "US"`.
*   **Ownership words.** The `ownership_words` list matches against `Person.role` or against the person's quote.
*   **`no_ai_visible`.** In order:
    1.  "unknown" if fewer than `no_ai_min_ok_pages` pages are `ok`;
    2.  otherwise "not_met" if any `ai_tool_markers` entry appears;
    3.  otherwise "met".
*   **Quote check.** `normalize_for_match()` applies NFKC normalization, replaces non-breaking and zero-width characters, collapses whitespace, straightens quotes and casefolds. A quote not found in the stored page text is dropped, and the verdict becomes "unknown".
*   **`check_claims`** (a function inside the drafter) flags digit runs and Title-Case words that aren't at the start of a sentence, unless they appear in one of:
    *   the lead's evidence quotes;
    *   the sender profile;
    *   the offer text;
    *   the company name;
    *   the plan's city;
    *   `TargetProfile.vocabulary`.
*   **Remembered answers map to tables.**
    *   The sender fields go to `sender_profiles` as a new row with `remembered=1`.
    *   The checklist goes to `checklists`.
    *   Supervisor keys go to `preferences`.
    *   `fill_from_memory` and the supervisor read the latest `remembered=1` row of each.
*   **Choosing the stop reason** (`finalize`):
    1.  `ParentState.stop_reason`, if set (`user_discarded`);
    2.  otherwise `complete`, if the target was met;
    3.  otherwise `decision_cap`, if it was reached;
    4.  otherwise `no_candidates`, if no `candidate` or `parked_no_email` rows exist;
    5.  otherwise `exhausted_rounds` or `exhausted_empty_round`, depending on `exhausted_reason`.

### Error policy

| Failure | Where it is caught | Result |
|---|---|---|
| Model refusal, `stop_reason == "max_tokens"`, or validation still failing after 1 retry, in triage, judgment or checklist | the `llm.py` helper returns `None` and logs `api_calls.stop_reason` | label `other` (`rejected_triage`) / `rejected_not_roofer` / verdict `unknown` |
| Same, in `parse_request` | `parse_request` | empty partial plan; intake asks for every field |
| Same, in the drafter | `drafter` | `drafts.status = draft_failed` |
| Claude router failure | `supervisor` | code router decides (`decided_by=fallback`) |
| Any 401/402 from Anthropic, Exa or Hunter, in any node | raised as `VendorAuthError` by `llm.py` and `clients/*`; caught by the CLI driver | `runs.status = paused` (no `finalize`; claims kept). Message per vendor, for example: "Exa rejected the API key (401). Check EXA_API_KEY in .env, then run `outreach resume`." |
| Anthropic 429 or 5xx | `ChatAnthropic(max_retries=2)` | if still failing: `VendorUnavailableError` → `paused` with "Anthropic is unavailable; run `outreach resume` later." |
| Exa or fetcher 429/5xx, Hunter 403/5xx (Hunter's rate-limit code is 403), connection errors | `tenacity`, `vendor_retry_attempts` tries, honouring `retry-after` | Exa or Hunter still failing: `VendorUnavailableError` → `paused`. A failed page fetch sets that page's `fetch_status` instead. |
| Hunter out of quota (HTTP 429, "usage limit reached"; see `docs/vendor_notes.md`) | `hunter_client` maps 429 to `HunterQuotaError` before `tenacity` sees it, so it is never retried | `qualified = 0`, `drop_reason = hunter_quota` |
| Hunter call cap reached | `hunter.py` | `qualified = 0`, `drop_reason = hunter_cap` |
| A send-guard check fails | `send_next` | `send_log(status='blocked', reason=<check>)` |
| SMTP error | `send_next` | `send_log(status='failed', reason=<error>)`; no retry |
| Any other exception | CLI driver | `runs.status = failed`; `resume` continues it |

### Task 0: Verify vendor facts before coding against them

Record each result in `docs/vendor_notes.md`. This file is created in phase 1, and phases 3b and 7 add sections to it. If a check fails, the affected item isn't built until the design has been changed, before the phase that uses it.

*   **Anthropic / langchain-anthropic:**
    *   `with_structured_output(..., method="json_schema", include_raw=True)` works on `claude-opus-5-5` and `claude-haiku-5-5` without forcing `tool_choice`, and the minimum langchain-anthropic version that supports it;
    *   effort can be set per instance;
    *   refusal and `max_tokens` show up as a `stop_reason` on the raw message, and where;
    *   whether thinking tokens count against `max_tokens` on 5.5;
    *   measured tokens per call site, used to size the `max_tokens` caps;
    *   thinking blocks are kept across tool turns;
    *   `max_retries` on `ChatAnthropic`.
*   **LangGraph:**
    *   a resumed node re-runs from its first line;
    *   `Command(goto)` can reach any node;
    *   `durability="sync"` on `stream()`;
    *   `recursion_limit` in the run config, whether subgraph steps count toward it, and whether it resets per `stream()`;
    *   how resume values are matched when a node calls `interrupt()` more than once;
    *   `get_state().tasks[*].interrupts`;
    *   `get_stream_writer`, plus the event shape `(namespace, mode, chunk)` for list `stream_mode` with `subgraphs=True`;
    *   `SqliteSaver(conn)` with `setup()`, and deleting a thread's checkpoints (`delete_thread`);
    *   checkpoint serializer types;
    *   `compile(checkpointer=False)` for subgraphs;
    *   `ToolNode(messages_key=...)`;
    *   a tool reading an injected `RunnableConfig`;
    *   the `langgraph-supervisor` claim (Alternative D).
*   **Exa:**
    *   the arguments to `search_and_contents`: `num_results` and its maximum, `exclude_domains` and its size limit, `category="company"`, `highlights`;
    *   the snake_case cost field name;
    *   the cost per call.
*   **Hunter:**
    *   the Domain Search fields (`sources`, `position`, `seniority`, `type`);
    *   the account and credits endpoint and the free-tier credits;
    *   the error codes, including the quota response.
*   **Mailtrap:** the sandbox SMTP host, the port, STARTTLS, and the limits.
*   **LangSmith:**
    *   `LANGSMITH_TRACING`;
    *   metadata and `run_name` on sub-invokes;
    *   thread grouping;
    *   flushing;
    *   free-tier retention.
*   **Libraries:**
    *   `tldextract`'s bundled snapshot with `suffix_list_urls=()`;
    *   `email-validator` with `check_deliverability=False` and `test_environment=True` for `*.test` domains;
    *   the Cloudflare `data-cfemail` XOR format;
    *   `pytest-socket`.
*   **Claims in earlier sections:** Alternative F's encrypted-content claim, and the cost figures in The Problem and Alternative E. The cost figures are rechecked at the phase 7 exit, not here.

### Target Files to Create/Modify

All paths are relative to the repository root `out-reach-bot/`. Every file is new, except item 58.

#### Project setup

1.  **`pyproject.toml`** (Create).
    *   **Build:** Python 3.12, hatchling. `src/outreach` is the package. Package data: `prompts/*.md`, `db/*.sql`, `db/memory_schema/*.sql`.
    *   **Dependencies:** `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-anthropic` (minimum version from Task 0), `langsmith`, `exa-py`, `httpx`, `tenacity`, `tldextract`, `selectolax`, `email-validator`, `phonenumbers`, `pydantic`, `typer`, `rich`, `python-dotenv`, `python-ulid`.
    *   **Dev dependencies:** `pytest`, `pytest-socket`, `ruff`, `mypy`.
    *   **pytest config:** registers the `live` marker.
    *   **Script:** `outreach = outreach.cli:app`.
2.  **`uv.lock`** (Create): generated and committed. It pins `tldextract`, and with it the public suffix snapshot that domain keys depend on.
3.  **`.env.example`** (Create), listing required and optional keys: `ANTHROPIC_API_KEY`, `EXA_API_KEY`, `HUNTER_API_KEY` (optional), `MAILTRAP_SMTP_USER`, `MAILTRAP_SMTP_PASS`, `LANGSMITH_API_KEY` (optional), `LANGSMITH_TRACING=true`, `LANGSMITH_PROJECT`, `OUTREACH_HOME` (optional).
4.  **`.gitignore`** (Create): `.env`, `*.db`, `*.db.bak*`, `.venv/`, `__pycache__/`.
5.  **`README.md`** (Create): setup, the commands, pauses and resume, the router modes, the do-not-contact list, the two databases, and a `sqlite3` query that lists parked companies.

#### Configuration

6.  **`config/settings.toml`** (Create). Every limit and switch is a key here, and module descriptions refer to these keys.

    | Group | Keys (defaults) |
    |---|---|
    | Run limits | `target_qualified=4`, `max_research_rounds=3`, `batch_size=10`, `max_companies_examined_per_round=30`, `max_decisions=20`, `max_questions=3`, `max_exa_searches_per_round=8`, `exa_num_results=10`, `max_hunter_calls_per_run=5`, `rejected_retry_days=90` |
    | Fetching | `extra_pages_per_company=3`, `page_text_chars=6000`, `snippet_chars=200`, `context_snippet_chars=200`, `max_redirects=5`, `connect_timeout_s=5`, `read_timeout_s=10`, `max_page_bytes=2000000`, `vendor_retry_attempts=3`, `per_host_delay_s=1.0`, `js_shell_min_chars=500`, `no_ai_min_ok_pages=2`, `user_agent="OutreachBot/0.1 (+https://github.com/<you>/out-reach-bot)"` (`config.py` refuses to start while it still contains `<you>`) |
    | Models | `model_bulk="claude-haiku-5-5"`, `model_quality="claude-opus-5-5"`, `llm_validation_retries=1`, `anthropic_max_retries=2` |
    | `[effort]` | `parse="low"`, `discovery="low"`, `triage="low"`, `judge="medium"`, `checklist="medium"`, `router="medium"`, `drafter="medium"` |
    | `[max_tokens]` | `parse=4000`, `discovery=8000`, `triage=2000`, `judge=8000`, `checklist=8000`, `router=8000`, `drafter=16000`. Adjusted from the Task 0 measurements. |
    | `[prices]` | input and output dollars per million tokens for each model, Exa dollars per call, Hunter credits per call |
    | Mail | `mail_smtp_host` (Mailtrap sandbox, copied from the sandbox's Integration → SMTP tab), `mail_smtp_port=2525`, `mail_starttls=true`, `mail_host_allowlist`, `mail_test_recipient`, `mail_from`, `mail_min_send_interval_s=10` (free sandbox: 1 email per 10 s, 50 a month, keeps the last 10) |
    | Graph | `recursion_margin=50`, `steps_per_company=6`, `steps_per_draft=3`, `outbox_passes=3` |
    | Editor | `editor=""`, `editor_early_return_s=2` |
    | Paths | `runs_db_path="%LOCALAPPDATA%\\outreach-bot\\runs.db"`, `memory_db_path="%LOCALAPPDATA%\\outreach-bot\\memory.db"` |
    | Other | `default_router="claude"` |
7.  **`config/target_roofing.toml`** (Create): the v1 `TargetProfile`. It holds:
    *   `query_templates` and `retry_query_templates`;
    *   `directory_blocklist`;
    *   `shared_host_patterns`;
    *   `ai_tool_markers` (`{name, html_pattern}`);
    *   `link_keywords`;
    *   `code_checks = ["no_ai_visible"]` and the phrases that map to each;
    *   `default_checklist`;
    *   `ownership_words`, `role_local_parts`, `placeholder_addresses`, `vendor_denylist`, `file_extension_tlds`, `vocabulary`;
    *   `question_templates` (one per `QuestionKey`);
    *   `offer_text`, `tone`, `max_words`, `signature_template`.
8.  **`config/fallback_cities.txt`** (Create): one `City, ST` per line. These are offered at the intake city question.

#### Package `src/outreach/`

9.  **`src/outreach/__init__.py`** (Create): defines `__version__`, which is the app version.
10. **`src/outreach/config.py`** (Create).
    *   **Finding files:** looks for `config/*.toml` and `.env` under `OUTREACH_HOME`. If that isn't set, it uses the repo root found from the package path.
    *   **Loading:** loads frozen `Settings` and `TargetProfile` objects. Path settings go through `os.path.expandvars`, and the data directory is created before any `sqlite3.connect`.
    *   **Checks:**
        *   `mail_smtp_host` against `mail_host_allowlist`;
        *   refuses to start while `user_agent` still contains `<you>`;
        *   reports every missing required key in one message (exit 3).
11. **`src/outreach/models.py`** (Create): the enums and Pydantic models listed in the Data model.
12. **`src/outreach/state.py`** (Create): `ParentState` (see the Data model), `ResearchState` (`discovery_messages` with `add_messages`, `cursor`), and `QualifyState` (`cursor`).
13. **`src/outreach/deps.py`** (Create): `Deps` holds `llms` (a dict from call site to `ChatAnthropic`), `exa`, `fetcher`, `hunter`, `mailer`, `runs_db`, `memory_db`, `clock`, `ids`, `settings` and `profile`.
14. **`src/outreach/llm.py`** (Create).
    *   **`build_llms(settings)`** creates one `ChatAnthropic` per call site, each with its model, effort, `max_tokens` and `max_retries=anthropic_max_retries`.
    *   **`structured_call(llm, schema, messages, call_site)`** returns `Model | None`. It:
        1.  reads through `llm_cache`;
        2.  calls `with_structured_output(schema, method="json_schema", include_raw=True)` [verify];
        3.  on a validation error, retries `llm_validation_retries` times with the error appended;
        4.  returns `None` on a refusal, on `stop_reason == "max_tokens"`, or when the retries run out;
        5.  maps 401/402 to `VendorAuthError`, and exhausted 429/5xx to `VendorUnavailableError`;
        6.  writes `api_calls` (including `stop_reason`) and `llm_cache`.
    *   Long text fields such as `Decision.reason` are cut in code, not limited in the schema.
15. **`src/outreach/tracing.py`** (Create).
    *   `run_config(run_id, router, status)` returns `{"configurable": {"thread_id": run_id}, "metadata": {...}, "recursion_limit": R}`, recomputed on each call.
    *   `child_config(round, lead_id, run_name)` gives the per-company config.
    *   `flush()` sends pending traces.
    *   Tracing is off when `LANGSMITH_API_KEY` is unset or `LANGSMITH_TRACING=false`.

#### Storage `src/outreach/db/`

16. **`src/outreach/db/__init__.py`** (Create).
17. **`src/outreach/db/schema_runs.sql`** (Create): DDL for the `runs.db` tables.
18. **`src/outreach/db/runs_db.py`** (Create).
    *   **Connections:** two connections to `runs.db`, each with `check_same_thread=False`, `journal_mode=WAL` and `busy_timeout=5000`. One goes to `SqliteSaver(conn)` with `setup()` [verify]; the other is used for app tables.
    *   **Methods:**
        *   `build_run_status`;
        *   rounds: `open_round`, `close_round`;
        *   caches: `cache_search_get`/`put`, `page_get`/`put` keyed by `(run_id, requested_url)`, `llm_cache_get`/`put`;
        *   candidates and qualifications: `queue_candidates` (`INSERT OR IGNORE`), `next_queued`, `set_candidate_status`, `write_qualification`, `delete_qualifications(lead_ids)`;
        *   drafts and review: `insert_draft`, `next_review_item`, `record_review`, `skip`;
        *   sending: `next_sendable`, `send_begin`, `send_complete`, `send_block`, `mark_sending_unknown`;
        *   run state: `record_answer`, `add_override`, `new_plan_version`, `record_decision` (it skips the write when the last row has the same `status_json`).
    *   **Test hook:** `fault_hook: Callable | None`, a test-only hook called inside `set_candidate_status`.
19. **`src/outreach/db/memory_schema/001_init.sql`** (Create): DDL for the `memory.db` tables.
20. **`src/outreach/db/memory_db.py`** (Create).
    *   **Connection:** `check_same_thread=False`, WAL.
    *   **`migrate(runs_db_path)`:**
        1.  Reads `PRAGMA user_version`.
        2.  If a migration is pending, opens `runs.db` read-only and refuses while a run is unfinished. A missing `runs.db` means there are no runs.
        3.  Copies the file to `memory.db.bak-<version>`.
        4.  Applies the pending files in one transaction.
    *   **Methods:**
        *   `should_skip`, `claim` (calls `should_skip` and refuses skipped domains), `finalize`, `release(run_id)`;
        *   `record_qualification`, `block_many`, `mark_contacted`;
        *   remember and recall helpers, per the table mapping in Rules;
        *   `purge`.

#### Graph `src/outreach/graph/`

21. **`src/outreach/graph/__init__.py`** (Create).
22. **`src/outreach/graph/build.py`** (Create): `build_graph(deps, stages=None, test_hooks=False)`.
    *   Adds the nodes and edges listed in How a run flows.
    *   `stages` replaces `run_research` and `run_qualify` with the canned versions from `tests/canned_stages.py` (item 52).
    *   With `test_hooks=True`, `confirm_plan` exits to `supervisor_override`, which returns `Command(goto="send_next")`.
    *   Compiles with the `SqliteSaver`. The subgraphs are compiled with `checkpointer=False` (items 30 and 39).
23. **`src/outreach/graph/intake.py`** (Create).
    *   **`parse_request`:** reads the `runs` row and makes a Haiku structured call returning `{city: str|None, target: int|None, items: [{text, code_check: str|None}]}`. A `code_check` that isn't in `TargetProfile.code_checks` becomes `None`. `city_override` replaces `city`. The result goes into `runs.parsed_json`.
    *   **`fill_from_memory`:** writes plan version 1 (`draft`) from `parsed_json`, the remembered rows and the defaults.
    *   **`intake_missing`:** stores a `PendingQuestion(origin="intake")` for the first missing field, then routes to `ask_user`, or to `confirm_plan` when nothing is missing.
    *   **`confirm_plan`:** a pause node with a self-edge. It applies the `edits` keys, freezes the plan, computes `checklist_key`, and saves the remembered items.
24. **`src/outreach/graph/pauses.py`** (Create).
    *   **`ask_user`:**
        1.  Shows the pending question.
        2.  Writes the answer to `answers`.
        3.  Applies the key's effect, as listed in "Questions the supervisor can ask"; intake keys write the next plan version.
        4.  Saves a remembered answer to its mapped table.
        5.  Clears the pending question.
        6.  Routes back by `origin`.
    *   **`review_next`:** as in How a run flows, step 7. The payload holds:
        *   the draft;
        *   its verdicts with quotes;
        *   the email and its source;
        *   the lead's real address next to the test recipient;
        *   `claim_flags`;
        *   next to the reject key, the note "blocks this company forever; use edit for a bad draft".

        When review ends, it calls `memory_db.block_many(this run's final rejects)`.
    *   **Shared helper:** validates the answer and re-interrupts on a mismatch.
25. **`src/outreach/graph/supervisor.py`** (Create).
    *   **`legal_moves(status) -> set[Move]`:** the truth table.
    *   **`ROUTES`:** maps `research` to `run_research`, `qualify` to `run_qualify`, `ask` to `ask_user`, `draft` to `drafter` and `finish` to `finalize`.
    *   **The `supervisor` node:**
        1.  Builds the status and the legal set.
        2.  Before asking anything, applies remembered supervisor answers.
        3.  If only one move is legal, takes it (`forced`).
        4.  Otherwise, calls the selected router, with the `CodeRouter` running as the shadow.
        5.  If the Claude router fails, or returns an illegal move, or returns `ask` with no key, uses the shadow's choice (`fallback`).
        6.  For `ask`, stores the `PendingQuestion` from the key's template.
        7.  Calls `record_decision`.
        8.  Returns `Command(goto=ROUTES[move])`.
26. **`src/outreach/graph/routers.py`** (Create).
    *   **`Router` protocol:** `decide(status, legal) -> Decision`.
    *   **`CodeRouter`:** the rule order in the Supervisor contract.
    *   **`ClaudeRouter`:** builds a Pydantic model for each call. `move` is a `Literal` of the legal moves. `question_key` is a `Literal` of `available_question_keys` when `ask` is legal, and `None` otherwise. It calls `structured_call(llms["router"], ...)`. Its prompt (`prompts/router.md`) holds the move definitions, the keys and their effects, the ask-or-decide rule, the number of questions left, and the `RunStatus`.
27. **`src/outreach/graph/outbox.py`** (Create).
    *   **`drafter`:** handles each draftable lead (see `undrafted_qualified`). It calls `structured_call(llms["drafter"], DraftOut)` with:
        *   the sender profile;
        *   the offer;
        *   the evidence quotes and verdicts;
        *   `greeting_name`.

        It then runs `check_claims` and `insert_draft` with `queue_pos` set in insertion order. A failure sets `draft_failed`.
    *   **`send_next`:** `next_sendable`, then the send guard, then `send_begin`, `mailer.send(...)` and `send_complete`. Headers:
        *   `From: <mail_from>`
        *   `To: <mail_test_recipient>`
        *   `X-Intended-Recipient: <lead email>`
        *   `Message-ID: <draft-{draft_id}@outreach-bot.local>`
28. **`src/outreach/graph/finalize.py`** (Create): applies the stop-reason rule and writes `runs.stop_reason`, and sets `runs.status` to `complete` (or `discarded`). It calls `memory_db.release(run_id)`, closes any open round, and returns the end-screen data.

#### Research `src/outreach/research/`

29. **`src/outreach/research/__init__.py`** (Create).
30. **`src/outreach/research/subgraph.py`** (Create). The researcher is compiled once with `checkpointer=False` [verify], and wrapped by `run_research`.
    *   **The wrapper:**
        *   re-enters an open round, or opens the next one (`open_round`);
        *   passes `round` in `config["configurable"]`, with `child_config`;
        *   writes the round's `stop_reason` and `new_companies` through `close_round`.
    *   **Nodes:**
        *   `seed_or_discover`: in round 1 with seeds, it claims the seeds and queues them, skipping discovery and triage;
        *   `discover` ↔ `ToolNode([exa_search], messages_key="discovery_messages")`, with a routing function on that key [verify];
        *   `triage_results`;
        *   `next_company` (picks carried-over queued rows first);
        *   `research_company`;
        *   `close_round`.
    *   **Progress:** emits `ProgressEvent`s through `get_stream_writer()` [verify].
31. **`src/outreach/research/discovery.py`** (Create).
    *   **`exa_search(query, objective, config: RunnableConfig)`** is built in `build_graph` as a closure over `deps`. `objective` is a required tool parameter, described with Exa's recommended wording (`exa_client.OBJECTIVE_DESCRIPTION`), and passed to `exa_client.search` unchanged. It reads `run_id` and `round` from `config["configurable"]` [verify]. For each call it:
        1.  checks `max_exa_searches_per_round` (it returns an error result when the limit is reached);
        2.  calls `exa_client.search(query, num_results=exa_num_results, objective=objective)` through the `search_calls` cache;
        3.  normalizes the domains and drops blocklisted, shared-host and `should_skip` ones;
        4.  queues the new ones with `INSERT OR IGNORE`, up to `max_companies_examined_per_round`;
        5.  returns rows of domain, title and highlight, cut to `snippet_chars`.
    *   **The discovery prompt** (`prompts/discovery.md`) holds the city, the templates for this round, and the queries already tried.
32. **`src/outreach/research/triage.py`** (Create): `classify_site(domain, title, snippet) -> (TriageLabel, confidence)`. One Haiku call per new domain, cached in `triage_labels`. A label other than `contractor_own_site` sets `rejected_triage` with `drop_reason=triage:<label>`.
33. **`src/outreach/research/domains.py`** (Create): `normalize_domain(url) -> DomainKey | Skip`. It lowercases, converts to IDNA, drops `www.`, and calls `TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)` using the bundled snapshot, and keys on `top_domain_under_public_suffix`. With private PSL entries on, a site-builder host such as `joe.wixsite.com` becomes its own registrable domain, so `shared_host_patterns` is matched against the whole **host** (`host.endswith("." + pattern)`). That catches private-PSL hosts and site builders the private list misses, such as `x.squarespace.com`. It also skips `facebook.com`. `NORMALIZATION_VERSION = 1`.
34. **`src/outreach/research/fetcher.py`** (Create): `fetch_page(domain_key, path) -> PageRecord`. This is the fetcher. It is not a model tool.
    *   **URL:** built from the claimed domain, https first, then http.
    *   **Redirects:** followed manually, up to `max_redirects` hops. Every hop resolves DNS and rejects private, loopback and link-local addresses (`private_ip`), and checks the domain (`offsite_redirect`). The DNS-rebinding window is accepted (Alternative O).
    *   **Limits:** the timeouts, `max_page_bytes`, `text/html` only, `user_agent` and `per_host_delay_s`.
    *   **Retries:** `tenacity` makes `vendor_retry_attempts` attempts on connection errors and 5xx.
    *   **Status rules:**
        *   `blocked`: 403 or 503, a page title of "Just a moment...", or a `cf-chl` marker;
        *   `js_shell`: visible text shorter than `js_shell_min_chars`;
        *   `soft_404`: a non-home path whose clean-text hash matches the home page's.
    *   **Cache:** reads through `pages` by `(run_id, requested_url)`.
35. **`src/outreach/research/links.py`** (Create): chooses up to `extra_pages_per_company` same-domain links from the header, nav and footer, scored by `link_keywords`. Falls back to `/contact` and `/about`.
36. **`src/outreach/research/extract.py`** (Create).
    *   **Email extraction methods** (on raw HTML):
        *   `visible_text`: excludes `<script>`, `<style>` and hidden elements;
        *   `mailto`;
        *   `jsonld`;
        *   `cf_decoded`: from `data-cfemail` or `/cdn-cgi/l/email-protection#HEX`, where the first byte XORs the rest [verify];
        *   `text_obfuscated`: forms like `name [at] domain [dot] com`.
    *   **Filters:**
        *   rejects `file_extension_tlds`, `placeholder_addresses` and `vendor_denylist`;
        *   `validate_email(addr, check_deliverability=False, test_environment=settings.testing)` [verify];
        *   tags `domain_relation`;
        *   keeps a context snippet of `context_snippet_chars`.
    *   **Also extracted:**
        *   US phone numbers, by the US-evidence rule;
        *   forms with email or message fields (`contact_form_url`);
        *   JSON-LD addresses;
        *   `ai_tool_markers` matches, stored in `pages.markers_json`.
37. **`src/outreach/research/judge.py`** (Create): one Haiku call per company over the clean text of its `ok` pages, `page_text_chars` each, inside `<page_content url=...>` delimiters, with no tools. The output is a `JudgeResult`, cached in `llm_cache`.
    *   Code checks every quote and drops claims that fail the check.
    *   Code sets the US evidence, then the status:
        *   `rejected_not_roofer`: no own-site quote, or the flag is false;
        *   `rejected_non_us`: no US evidence;
        *   `candidate`: an on-site email exists;
        *   otherwise `parked_no_email`.

#### Qualify `src/outreach/qualify/`

38. **`src/outreach/qualify/__init__.py`** (Create).
39. **`src/outreach/qualify/subgraph.py`** (Create): the qualifier, compiled once with `checkpointer=False`, and wrapped by `run_qualify`. It processes one lead per step: `score_lead` → `find_email` → `decide`. `decide` always writes `qualifications`, and calls `memory_db.record_qualification`. It emits `ProgressEvent`s.
40. **`src/outreach/qualify/scoring.py`** (Create).
    *   **Items checked by code** use the `code_check` registry.
    *   **`llm` items** get one Haiku `structured_call` per lead (cached). A quote not found in the stored page text becomes "unknown".
    *   **`points = count(met)`.**
    *   **`rescore(run_id, checklist_version)`** recomputes `qualified` with no model call. It returns the leads that now pass and have no on-site email, so that `lower_threshold` can delete their rows.
41. **`src/outreach/qualify/email_choice.py`** (Create): `name_match` and `choose_email`, following the Rules. `choose_email` sets `email_type` and `greeting_name`.
42. **`src/outreach/qualify/hunter.py`** (Create).
    *   **When it runs:** only for a lead with no `same_domain`/`freemail` observation and `points >= N`.
    *   **Before calling:** checks `max_hunter_calls_per_run` (if reached, `drop_reason=hunter_cap`) and the remaining credits from `account()`.
    *   **Cache:** reads through `hunter_cache`.
    *   **What it keeps:** only addresses with a source URL, stored as `email_observations(method='hunter')`.
    *   **Never** calls Email Finder.

#### Clients `src/outreach/clients/`

43. **`src/outreach/clients/__init__.py`** (Create): defines `VendorAuthError`, `VendorUnavailableError` and `HunterQuotaError`.
44. **`src/outreach/clients/exa_client.py`** (Create): `search(query, num_results, objective)` calls exa-py's `search(query, type="auto", num_results=num_results, objective=objective, category="company", exclude_domains=<blocklist, trimmed to 1200 entries>, contents={"highlights": True})`. This follows Exa's build-with-exa skill: bare highlights, with `snippet_chars` applied in code, and an `objective` on every call. `search_and_contents` is deprecated in exa-py 2.28 (`docs/vendor_notes.md`, Phase 3b). `category="company"` rejects published-date filters, so none are sent. A unit test pins the request body against the real exa-py. It maps errors and records `api_calls`, using `response.cost_dollars.total`.
45. **`src/outreach/clients/hunter_client.py`** (Create): `domain_search(domain)` calls `GET /v2/domain-search` with `limit=10` (free plans return `pagination_error` above 10), and `account()` calls `GET /v2/account` (no credit charge) and reads `requests.credits.remaining`. Both use `httpx`.
    *   **Errors:** 429 (quota) maps to `HunterQuotaError` before `tenacity` sees it. 403 (rate limit) and 5xx are retried. 401 maps to `VendorAuthError`.
    *   **Fields:** the source URL is `sources[].uri`. `type`, `seniority` and `position` are as named in the Rules.
    *   Records `api_calls`.
46. **`src/outreach/clients/mailer.py`** (Create): `SandboxMailer` uses `smtplib` with STARTTLS and login. The constructor refuses a host that isn't on the allowlist. `send(recipient, subject, body, headers)` refuses any recipient other than `mail_test_recipient`, and always uses `mail_from` and `mail_test_recipient` as the envelope sender and recipient.
    *   **Pacing:** before each send it waits until `mail_min_send_interval_s` has passed since the latest `send_log.finished_at` in `runs.db` (any run). The interval is read from the database rather than kept in memory, so it also holds after a resume. Without it, the second send hits the sandbox rate limit and the draft is marked `failed` with no retry. The wait uses an injectable clock, so tests don't sleep.

#### CLI and UI

47. **`src/outreach/cli.py`** (Create): the Typer app.
    *   **Commands:**
        *   `run [PROMPT] [--router claude|code] [--seeds FILE] [--city X]`
        *   `resume [--router X]`
        *   `discard`
        *   `leads --status parked|blocked|contacted`
        *   `purge DOMAIN`
    *   **Driver** (pseudocode):

        ```text
        unfinished = runs where status in (active, paused, failed)
        if unfinished:
            show banner; choose resume | start new | discard
            if start new or discard: discard(unfinished)          # always allowed
            if resume: check_versions() (exit 4 on mismatch); check --router == runs.router (exit 2)
        migrate memory.db                                          # only pending migrations
        if new: run_id = ULID(); insert runs(active, prompt, router, seeds_json, city_override)
                input = {"run_id": run_id}
        else:   runs_db.mark_sending_unknown(run_id); input = None   # resume from checkpoint
        try:
            loop:
                cfg = tracing.run_config(run_id, router, runs_db.build_run_status(run_id))
                for ns, mode, chunk in graph.stream(input, cfg, durability="sync",
                                                    stream_mode=["custom","updates"], subgraphs=True):
                    if mode == "custom": render ProgressEvent(chunk)
                interrupts = graph.get_state(cfg).tasks[*].interrupts
                if none: render end screen from finalize's data; break   # finalize wrote status
                payload = PausePayload(**interrupts[0].value)
                answer = screens.ask(payload)                             # editor for review edits
                set runs.status = active
                input = Command(resume=answer.model_dump(mode="json"))
        except VendorAuthError | VendorUnavailableError as e:
            set runs.status = paused; print e.message; exit 1
        except KeyboardInterrupt:
            set runs.status = paused; print "Progress saved. Resume with `outreach resume`."; exit 130
        except Exception:
            set runs.status = failed; print trace path; exit 1
        finally:
            tracing.flush()
        ```
    *   **`recursion_limit`** is `recursion_margin + max_research_rounds × max_companies_examined_per_round × steps_per_company + qualified_count_or_target × steps_per_draft × outbox_passes`. It is recomputed for every `stream()` call [verify how steps are counted].
    *   **`resume`** fails with exit 2 if no run is unfinished.
    *   **`discard`:**
        1.  sets `discarded`;
        2.  calls `memory_db.release`;
        3.  deletes this thread's checkpoints [verify `delete_thread`].
    *   **`purge`** is refused while a run is unfinished.
    *   **Exit codes:** 0 done, 1 error or vendor pause, 2 bad input, 3 config error, 4 version mismatch, 130 Ctrl+C.
48. **`src/outreach/ui/__init__.py`** (Create).
49. **`src/outreach/ui/screens.py`** (Create): Rich rendering of these screens:
    *   **plan:** the editable fields with their `edits` keys, the item check types, the threshold, and the "remember this" choices for `sender` and `checklist`;
    *   **question:** the pending question;
    *   **review:** "Draft k of n", the verdicts, the email source, the real address next to the test recipient, the flags, and the keys with the reject note;
    *   **progress:** one line per `ProgressEvent`;
    *   **end screen:** the funnel, the stop reason text, "`qualified_count` of `target_qualified`", blocked and failed sends, and the assumptions.
50. **`src/outreach/ui/editor.py`** (Create).
    *   **Choosing the editor:** `VISUAL`, then `EDITOR`, then `settings.editor`, then `code --wait`, then `notepad`. Each candidate is found with `shutil.which` (on Windows, `code` resolves to `code.cmd`).
    *   **The temp file:** UTF-8 without a BOM, created with `delete=False`, in this format: a `Subject:` line, a blank line, the body, then the marker line `#--- outreach: everything below this line is ignored ---` followed by instructions. Line endings are normalized when the file is read.
    *   **Checks after the editor returns:**
        1.  If the editor exits within `editor_early_return_s` and the file is unchanged, ask for Enter, then read the file again.
        2.  An empty or unchanged file then means "cancelled".
    *   **After Ctrl+C:** the temp file is kept, and its path is printed.

#### Prompts `src/outreach/prompts/`

51. **`parse_request.md`, `discovery.md`, `triage.md`, `company_judge.md`, `checklist_judge.md`, `router.md`, `drafter.md`** (Create). The `prompt_version` is the file's sha256. Prompts that see web text treat it as data. The drafter prompt says to use only the facts it is given, and states `max_words`, the tone and the greeting.

#### Test support and examples

52. **`tests/canned_stages.py`** (Create): canned `run_research` and `run_qualify` nodes used in phase 3, passed in through `build_graph(deps, stages=...)`. They write fixture rows to `run_candidates`, `research_rounds` and `qualifications`, and return `StageResult`s. Phases 4 and 5 replace them with the real wrappers.
53. **`examples/tucson_demo.txt`** (Create): a seeds file with one domain per line; `#` lines are ignored. Kept local and gitignored since 2026-11-14, because it lists real businesses; the published template is `examples/seeds.example.txt`. These are real public business domains, for live demos only. It is not used in tests.

#### Tests `tests/`

54. **`tests/conftest.py`** (Create):
    *   blocks sockets with `pytest-socket` (`--disable-socket`), except for tests marked `live`;
    *   sets `LANGSMITH_TRACING=false` and `settings.testing=True`;
    *   provides the `deps_fake()` and `graph_fake()` fixtures.
55. **`tests/fakes.py`** (Create):
    *   **`FakeChat`:** scripted structured outputs, tool calls, refusals and `max_tokens` stops, with a counter per call site;
    *   **`FakeExa`:** results, an empty response, an all-directory response, 401 and 429, with a call counter;
    *   **`FakeFetcher`:** fixture HTML, statuses, timeouts and redirects, with a call counter;
    *   **`FakeHunter`:** credits, sources and a quota response, with a call counter;
    *   **`RecordingMailer`**;
    *   **`FakeClock`**.
56. **Fixtures:**
    *   **`tests/fixtures/sites/*.html`**, on `*.test` domains:
        *   `mailto:`-only pages;
        *   Cloudflare span and link forms, generated by an encoder that mirrors the decoder;
        *   a Gmail address on the company's own site;
        *   an owner with a matching email;
        *   two owners;
        *   junk addresses;
        *   a chat widget, and a site with no widget;
        *   a JavaScript shell;
        *   a bot-challenge page;
        *   a directory page and a "top 10" page;
        *   a franchise page;
        *   an injection page;
        *   a soft-404 page.
    *   **`tests/fixtures/exa/*.json`**, including an all-directory response.
    *   **`tests/fixtures/hunter/*.json`**: with and without sources, executive, generic, and a quota response.
57. **Test files.** All run offline except `live`.

    | File | What it covers |
    |---|---|
    | `test_domains.py` | the normalization table, shared hosts, IDNA |
    | `test_extract.py` | each method, including the Cloudflare round trip, the junk filters and hidden text |
    | `test_name_match.py` | each form; the "contains" and first-name-only cases |
    | `test_email_choice.py` | the preference order; staff addresses never chosen; Hunter executive type; greeting name; `test_unsourced_address_rejected` (G5) |
    | `test_scoring.py` | points against N; unknown handling; the `no_ai_visible` clause order; `rescore` with no model calls and the leads it returns |
    | `test_hunter.py` | the trigger, the cap being hit with more leads queued (rows still written, `hunter_cap`), the quota response mapped to `hunter_quota`, the source filter, the cache |
    | `test_fetcher_guard.py` | path-only URLs; a private IP on the first hop and on a redirect hop; offsite redirects; the status rules |
    | `test_legal_moves.py` | every truth-table row; the start state; an all-filtered round counted as empty; a seeds run reaching round 2; the decision cap; the set is never empty; `available_question_keys` conditions |
    | `test_routers.py` | the code router order; Claude illegal move → fallback; refusal → fallback; `ask` with no key → fallback; at most 2 model calls per turn |
    | `test_router_calls.py` | a scripted Claude run where router calls happen only on turns with more than one legal move (G2); CUJ 4.2 with `extra_round`, `change_city` and `lower_threshold` effects |
    | `test_router_prompt.py` | the router prompt never contains page text |
    | `test_llm.py` | refusal and `max_tokens` → `None`; the validation retry; cache hits; 401 → `VendorAuthError` |
    | `test_pauses.py` | `test_resume_same_pause_id`; a mismatch re-interrupts; `test_plan_edit` (edits keys); `test_resume_at_plan`; `test_remembered_not_asked` (G9) for an intake key and a supervisor key; the question cap; the code router never asks |
    | `test_resume_review.py` | CUJ 2: interrupt at draft 2, rebuild from the same files, same `pause_id` and text; skip; back over skips; back on the first draft; back over a reject leaves no block; nothing sent before review ends |
    | `test_resume_research.py` | CUJ 3: `runs_db.fault_hook` raises inside `set_candidate_status` for company k, after the judge call and before the evidence write. Resume. No call that finished before the crash is repeated, and call totals equal an uninterrupted fixture run plus the one call that raised. The final rows are equal. |
    | `test_send_guard.py` | definition of done: `build_graph(test_hooks=True)`, so the run reaches `send_next` with an unreviewed draft. Expect 0 sends and a `blocked` row (check 1). A hash mismatch is `blocked` (check 2). A contacted domain is `blocked` (check 3). The recipient is always the test address. `sending` becomes `unknown` on resume and is never retried. |
    | `test_mailer.py` | refusal of a host off the allowlist; refusal of other recipients; `From`, `X-Intended-Recipient` and `Message-ID` headers |
    | `test_injection_research.py` | the injection fixture changes no outcome that code enforces |
    | `test_memory_db.py` | migration with a backup; refusal while a run is unfinished only when a migration is pending; a missing `runs.db`; the `should_skip` table; `claim` refusing skipped domains; reject → future block; `purge` |
    | `test_cli.py` | `test_version_mismatch_refuses` (exit 4) while `discard` still works; `test_discard_releases_claims`; `--router` mismatch on resume; `purge` refused during a run; a vendor auth error → `paused` and resumable; a `failed` run is resumable |
    | `test_editor.py` | file format, the marker, cancellation, the early-return Enter prompt, line endings, `shutil.which` lookup |
    | `test_end_to_end_fake.py` | code router: CUJ 1 steps 1–5 and 7, with step 6 checked as recorded mailer calls; CUJ 4 steps 1–3; CUJ 3; CUJ 5. Asserts the funnel, the stop reasons and "`qualified_count` of `target_qualified`". |
    | `live/test_smoke.py` | opt-in (`OUTREACH_LIVE=1`, marker `live`): 1 search, 2 companies, real APIs, LangSmith traces grouped by `run_id` |

#### Scripts and docs

58. **`docs/designs/outreach_bot.md`** (Modify): this document. Status set to `Approved` on 2026-11-13.
59. **`scripts/probe_exa_hunter.py`** (Create). Runs in phase 3b and uses `exa_client.search(query, num_results=25)`.
    *   It runs 3–5 query wordings across 3 cities. For each query it counts how many of the 25 results are roofers' own sites, and how many of those show an email.
    *   It runs Hunter Domain Search on 10 form-only domains.
    *   **Go/no-go:** if the average is below 5 of 25, it's a **no-go**. v1 then runs only with `--seeds`, and a different source becomes a v1.1 decision.
60. **`docs/vendor_notes.md`** (Create): created in phase 1 with the Task 0 results. Phase 3b adds the probe results, and phase 7 adds the cost recheck.

### Build order and phase exit tests

Status is updated as each phase's exit tests pass (last updated 2026-11-13).

| Phase | Status | Files | Exit tests |
|---|---|---|---|
| 1. Task 0 | **Complete** | 60 | every Task 0 item recorded; design changes made for failed checks |
| 2. Foundation | **Complete** | 1–20, 54, 55 | `test_memory_db`; `test_llm` (refusal and `max_tokens` → `None`, retry, cache, 401) |
| 3. Thin graph | **Complete** | 21–25, 26 (`Router` protocol and `CodeRouter`), 27, 28, 46, 47–51, 52 | `test_legal_moves`, `test_pauses`, `test_resume_review`, `test_send_guard`, `test_editor`, `test_mailer`, `test_cli` |
| 3b. Probe | **Complete** | 43–45, 59 | go/no-go recorded in `docs/vendor_notes.md` |
| 4. Researcher | **Complete** | 29–37, 56 (sites, Exa) | `test_domains`, `test_extract`, `test_fetcher_guard`, `test_resume_research`, `test_injection_research` |
| 5. Qualifier | **Complete** | 38–42, 56 (Hunter) | `test_scoring`, `test_name_match`, `test_email_choice`, `test_hunter` |
| 6. Claude router | **Complete** | 26 (`ClaudeRouter`), tracing metadata | `test_routers`, `test_router_calls`, `test_router_prompt` |
| 7. End to end | **Complete** | 5, 53, 58 | `test_end_to_end_fake`, `live/test_smoke`, the manual check below, and the cost recheck recorded in `docs/vendor_notes.md`. Manual check passed: 8 approved drafts arrived in the Mailtrap sandbox, and LangSmith grouped the run under its run ID. |

### Verification Plan

*   **Build:** `uv sync`, then `uv run outreach --help`.
*   **Tests:** `uv run pytest`. Network access is blocked except for tests marked `live`.
*   **Static analysis:** `uv run ruff check .` and `uv run mypy src/`.
*   **Definition of done:** `tests/test_resume_research.py` and `tests/test_send_guard.py` both pass.
*   **Coverage requirement.** At least one named test in the table above must exercise each of these:
    *   every truth-table row;
    *   every `ExtractMethod`;
    *   every send-guard check;
    *   every `PausePayload` kind and `ReviewAction`;
    *   every Error policy row.
*   **Manual check:** one live run of `outreach run "find me leads in Tucson that don't use AI"` against the Mailtrap sandbox, with LangSmith on. Confirm that:
    *   approved drafts arrive;
    *   no other mail was sent;
    *   the end screen's numbers match `runs.db`;
    *   LangSmith groups the runs by `run_id`.
*   **Router comparison:** run CUJ 6, then compare `chosen_move` and `shadow_move` in `supervisor_decisions`.

--------------------------------------------------------------------------------

<!-- Provenance: Indicates initial structural drafting via EGM. -->

*Drafted with EGM*
