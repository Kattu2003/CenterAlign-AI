# PROGRESS.md

Running log kept by the builder (Claude Code) while executing `plan.md`, phase by phase.
Each entry: what is done, what is broken, open questions, and decisions the plan didn't specify.

## Build environment notes

- Built in a Linux cloud container: Python 3.13, Playwright 1.56.0 with its pinned Chromium (1194).
- **No LLM API key was available in the build environment.** Everything that does not need a model
  (sandbox, gateway, intents, approvals, queue, worker, verifier deterministic checks, reports,
  red-team scenario) is verified with unit tests and `ScriptedLLM` browser integration tests.
  Scenarios that need a real model (intake quality, the agent's own decisions, the eval table) must be
  run by the author with a key in `.env` — see "Open questions" at the end.
- Branch: everything is on `main`, one commit per phase with the plan's commit messages. It was built on a session
  branch and moved to `main` with its history unchanged, so the hashes cited in the docs (phase 9 = `bb69d57`) hold.

## Phase 0 — Scaffold

Done:
- `pyproject.toml` (deps from plan §5, console script `karya`), `.env.example`, `.gitignore`.
- `karya/config.py` (frozen dataclass settings from env/.env; relative paths resolve against the repo root).
- `karya/db.py`: every table from plan §10, WAL + busy_timeout + foreign keys, `transaction()` with
  `BEGIN IMMEDIATE`, human-readable id counters, append-only `audit` helper.
- `karya/cli.py` skeleton with the full command surface; doc stubs.

Decisions not in the plan:
- `pytest` is in the main dependency list so a clean `pip install -e .` can run the tests.
- `audit` table has SQLite triggers that abort UPDATE/DELETE, so "append-only" is enforced by the
  database, not only by the absence of code paths.
- `intents` has two extra columns: `approval_id` (which approval a write used, so it can be consumed)
  and `note` (human-readable outcome detail). `tasks` has `last_error`.
- A `counters` table allocates `T-0001` / `APR-0001` / `Q-0001` / `F-0001` ids inside transactions.
- All timestamps use one UTC format (`2026-10-08T09:12:33.123Z`) so string order equals time order
  (lease expiry is a string comparison).
- The `schedules` table (P2) is not created; it would come with the P2 scheduler.
- `.env.example` is kept verbatim from the plan, but python-dotenv reads `LLM_MODEL=   # comment`
  as the literal comment text. `karya.config.env_str` therefore treats any value starting with `#`
  as empty (the sandbox settings reuse it).

Acceptance: `pip install -e .` and `karya --help` work; `python -c "import karya.db as d; d.init()"`
creates `data/karya.db` with every table; deleting from `audit` is rejected by the trigger.
(`playwright install chromium` was not needed here: the container ships Chromium 1194, which
matches Playwright 1.56.0.)

## Phase 1 — Sandbox company

Done:
- `sandbox/common/`: env-driven settings (temp dirs in tests), integer-paise money helpers with Indian
  grouping, relative dates (`D-3`), session login + CSRF, chaos middleware + admin router, shared
  templates/stylesheet, app factory.
- Meridian ERP (8101) and Meridian Mail (8102) per plan §7.3–7.4, including the read-only `X-API-Key`
  APIs, the bank-change and create-vendor forms (so AP-002/AP-003 are demonstrable) and app-side audit.
- Seed data (`sandbox/seed/data.yaml`), 8 invoice PDFs in 3 layouts / 4 date formats / 3 amount
  formats, exact GST arithmetic, the 6pt grey injection on AF-3307, and a pypdf self-check on generation.
- `sandbox/run.py` (`up/down/reset/status`, chaos profile, arm/disarm) + `karya sandbox ...` commands.
- `tests/test_sandbox.py`: 14 in-process tests covering every §7.8 acceptance item.

Acceptance: `karya sandbox reset && karya sandbox up && karya sandbox status` → both apps up (also
from another working directory); all §7.8 checks covered by tests and pass; CLI `arm`/`chaos`
verified against the live apps; `reset` clears chaos on running apps.

Decisions not in the plan:
- The sandbox never imports `karya` (it is the environment, not the runtime); the CLI imports
  `sandbox` lazily so the runtime never depends on it.
- Cookie names differ per app (`erp_session`, `mail_session`): cookies are per host, not per port,
  so apps on 127.0.0.1 would otherwise overwrite each other's sessions.
- After a lost session, a POST redirects to `/login?next=<the form it came from>` (taken from the
  Referer), so the agent lands back on the empty form and must refill it.
- `session_expired`: profiles only expire sessions on form GETs; armed faults can also hit POSTs.
  `/login` and `/logout` are never chaos targets for session expiry.
- `commit_then_504` is implemented generically: the middleware lets the app run, and if the app
  returned success (< 400) it swaps the response for a 504. A failed validation (400) does not consume
  the armed fault.
- The mail API does not expose the seed keys (`M1`…`M9`); only eval checks use them, via the DB.
- `seed_meta.json` records the D used at reset, so ground-truth checks resolve `{D+27}` against the
  same day even if a run crosses midnight.
- `reset` regenerates `sandbox/shared_drive/finance/` and empties `outbox/` (generated sandbox state,
  as the plan's `reset` description says); nothing outside the sandbox is touched.
- IST is a fixed +05:30 offset in the sandbox (India has no DST), avoiding a `tzdata` dependency on Windows.

## Phase 2 — Company configuration

Done:
- `company/`: `company.yaml`, six handbook SOPs chunked on `##` (each AP-01 step is its own chunk, so
  `ap-01#5` is the duplicate check), `policies.yaml` (AP-001/002/003), connectors `erp`, `mail`,
  `shared_drive`, schema `invoice.json`. The handbook deliberately never says what "Acme" means.
- `karya/connectors/models.py` (strict pydantic specs, path-template matching, origin normalisation),
  `loader.py`; `karya/gateway/policy.py` (structured conditions, AND, deny > approval > allow, every
  matching rule recorded); `karya/company.py` (one `Company` object + cross-reference `problems()`).
- `karya config check`: summary tables (people, handbook, connectors, declared actions with risk,
  API endpoints, rules, schemas, which credential env vars are set) and a non-zero exit on problems.
- Tests: `tests/test_policy.py` (every op, AND, precedence, matching, fail-closed),
  `tests/test_connectors.py` (templates, unknown writes, blocked globs, origins, handbook chunks).

Decisions not in the plan:
- `karya/company.py` (not in the plan's layout) aggregates company.yaml, handbook, policies,
  connectors and schemas so the rest of the runtime takes one object.
- Fail closed in the policy engine: a condition whose field is missing or cannot be coerced counts as
  matching (so a malformed amount still triggers AP-001). String comparisons are case-insensitive.
- `config check` also validates cross-references (rule → connector/action/field, approver role held
  by someone, reconcile api declared, template fields known) — the likely live-edit mistakes.
- The overview doc's id is `company-overview` (no `xx-NN` prefix in its file name).
- Connector specs are strict (`extra="forbid"`), so a typo in YAML fails loudly instead of being ignored.
- Extra declared fields on `update_vendor_bank` / `create_vendor` so denied attempts are audited with
  their payload.

## Phase 3 — Core runtime

Done:
- De-risk: `route.fetch(max_redirects=0)` + `route.fulfill(response=resp)` makes Chromium follow the 303
  after a form POST (verified before building the gateway on it). `page.goto` on an attachment raises
  "Download is starting"; the browser tool handles that as a download.
- `karya/llm/`: protocol + OpenAI/Azure + Anthropic clients (explicit retries, one tool per step),
  `structured()` forced-function outputs with one validation retry, `ScriptedLLM` (+ `find_ref`).
- `karya/tools/`: browser (Playwright, snapshot.js refs, downloads, GET retries, logged-out hints, stale
  refs, password refusal, login by tool), api_get, files_* (virtual roots, sandboxed, extraction →
  code normalization → schema checks), memory_search/record_fact, ask_human, update_plan, finish,
  submit_verdicts; explicit registry.
- `karya/gateway/`: full §11.7 pipeline (`decide()` is unit-testable), intents state machine +
  reconciliation, payload-bound single-use approvals with RBAC, secrets resolution + redaction.
- `karya/runtime/`: models, ledger (append-only, `trace.jsonl` mirror, state rebuild), intake, context
  builder, loop, stuck detection; Phase 3 placeholder verifier/report (replaced in phase 4).
- `karya/worker/queue.py` (leases) and `process_task` with a heartbeat; `karya run` (live stream,
  waits and resumes when a person answers/approves), `karya resume`, `karya config llm`.
- Tests (105 + 1 skipped): gateway decision pipeline (17), intents, parsing, file sandbox, ledger
  rebuild, memory retrieval, injection heuristics, redaction, and two real-browser scripted runs:
  the full happy path and the Vertex approval hold.

Acceptance:
- Unit tests for gateway, policy, intents, parsing, files: pass.
- Happy path: verified end to end with `ScriptedLLM` through real Chromium, the gateway and the sandbox
  apps (one ERP record V001/AI/2026/0917 = 42380.00, D-3, D+27, entered by karya.agent; M2 labelled
  Processed; Goal Spec, plan and a `why` on every step in the ledger; screenshots; trace.jsonl).
  **Not run with a real LLM** (no API key in the build environment) — run
  `karya run "Find the latest invoice from Acme Industries in the AP mailbox, enter it into the ERP, and tell me when it's done."`
  once `.env` has a key (`karya config llm` checks the key first).
- Vertex: the 202 "Held for approval" page and a pending APR row (role finance_manager, rule AP-001),
  task `waiting_approval`, lease released, nothing posted — verified by a scripted browser test.

Decisions not in the plan (see DECISIONS.md for the major ones):
- Stateless steps: every step is `system + one user message` rebuilt from the ledger (no provider tool
  transcripts). Same code path for live runs and resumes; trivially portable across providers.
- The gateway runs the duplicate check BEFORE the approval hold, so nobody is asked to approve a
  duplicate; for writes with a reconcile spec the system of record (not the status code) decides
  `committed`, and the same lookup returns the record reference (ERP-INV-000104).
- A 3xx to the connector's `logged_out_signal` path on a write is `failed` ("session expired, nothing
  submitted"), not `committed`. If the pre-write existence check itself fails, the write is not sent
  (503, fail closed). Identical writes already in flight are refused (double-submit guard).
- `Observation.meta` (extra field): trusted notes/hints, retries, relogin markers, kept out of the
  `<untrusted>` content.
- `files_extract` auto-records `extracted:<path>` as a task fact so the verifier/report can compare the
  ERP record with the document.
- `why` longer than 200 chars is truncated, not rejected (a rejected step wastes a model call).
- Blocked sub-resource requests are aborted and audited without an observation event (no noise).
- `browser_fill`/`browser_select` return a refreshed snapshot (no screenshot) so the latest snapshot
  always shows the current field values; refs stay stable because the DOM did not change.
- Anthropic: the system prompt (instructions + company context) is marked for prompt caching.
- `karya/cli_display.py` (not in the plan's layout) holds the rich rendering for the CLI.

## Phase 4 — Verifier and report

Done:
- `karya/runtime/verifier.py`: deterministic system evidence first (every committed write with a reconcile
  spec is re-looked-up and compared field by field; matching records counted; extracted document values,
  approvals and policy events listed), then a separate read-only model conversation (own browser context,
  gateway `read_only`, max 15 steps, never sees the worker transcript). Overall verdict computed in code:
  pass only if every criterion passes and the system evidence shows no mismatch.
- `karya/runtime/report.py`: report.md/report.json with outcome, request, what I did (with `why`), results,
  verification table + system evidence, needs your attention, flags, evidence (screenshots, record refs,
  files), company knowledge used, stats; the episode is saved for memory.
- `karya show <T-id>` (rendered report) and `karya show <T-id> --trace` (timestamps, args redacted, why,
  observation, notes, artifacts, policy events).
- `ScriptedVerifier` for tests.

Acceptance (scripted, real browser): happy path report shows c1–c3 pass with the ERP ref from the API
lookup and screenshot ids; corrupting the ERP amount in a `before_verify` hook makes c2 fail
("amount submitted 42380.00 but Meridian ERP has 43280.00"), the loop receives verifier feedback, and the
task ends `partial` after the second failed round; `--trace` prints the timeline.

Decisions not in the plan:
- The overall verdict is computed in code, never taken from the model; system-evidence mismatches
  (missing record, duplicates, field mismatch) force `fail`.
- Missing verdicts become `unverifiable` (and therefore not a pass).
- If the verifier model errors, the result is an honest `fail` with the error in the notes.
- "Company knowledge used" in the report lists learned facts that were in the model's context
  (deterministic, from retrieval notes), so citing a fact never depends on the model remembering to.
- Episodes are written by the report step (one per finished task), already in phase 4.

## Phase 5 — Queue, worker, human-in-the-loop

Done:
- `karya/worker/worker.py`: `run_worker` (claim → run → sleep when idle; `--once`), heartbeat thread
  renewing the 60 s lease every 20 s, first Ctrl-C = finish the current step, checkpoint, release (task
  back to `queued`), second Ctrl-C = exit now; per-task `abort` when the lease is lost.
- `karya/worker/inbox.py`: answer questions and decide approvals (RBAC via approvals.decide), append the
  decision to the ledger, requeue the task — shared by the CLI and (later) the eval auto-responder.
- CLI: `submit [--at ISO] [--max-steps] [--priority]`, `worker [--once] [--id]`, `tasks [--status]`,
  `inbox`, `answer [--by] [--no-remember]`, `approve --by [--note]`, `reject --by --reason`, `audit`.
- Tests: queue units (exclusive ordered claims, run_at, lease-expiry reclaim, attempts cap, retries,
  suspend/requeue, non-owner release ignored), worker SIGINT in a real subprocess, and five real-browser
  scripted flows: approval with RBAC refusal → single post → approval consumed; rejection → escalated
  with the reason in the report; simulated `kill -9` after a write → lease expiry → another worker
  reclaims and resumes (fresh browser, resume note, step numbers continue, a re-submission is blocked
  as a duplicate, exactly one ERP record); ask_human → waiting_human → answer → resumes with the answer
  in context; Ctrl-C mid-task → step checkpointed, task back in the queue.

Acceptance: every Phase 5 item is covered by the tests above; CLI `inbox`/`approve` (refused for Arjun Rao,
accepted for Neha Kulkarni)/`tasks`/`audit` also checked by hand against the live sandbox.
Not run: the CLI worker with a real LLM (no key here).

Decisions not in the plan:
- `attempts` counts failed/crashed runs, not suspensions: a clean suspend or finish resets it, so a task
  that waits for approval several times is never failed for "too many attempts".
- A rejection requires a reason (it is reported and later learned from).
- `answer --by` must be a person in company.yaml (default: the task's requester).
- `karya run` keeps waiting while a task needs a person and resumes in the foreground when it is
  requeued; `karya resume <T-id>` continues a queued/waiting task (the plan's cut-list fallback, kept as
  a convenience).

## Phase 6 — Reliability hardening

Done (`tests/test_reliability.py`, 11 real-browser scripted runs with armed faults):
- `commit_then_504` → the gateway reconciles ("returned HTTP 504, but the record exists
  (ERP-INV-000104). The write succeeded; do not resubmit"), intent `committed` + `reconciled=1`,
  exactly one record; a model that resubmits anyway is blocked ("Already done in this task").
- `write_conflict_409` → intent `failed` ("Nothing was saved"), the resubmission commits → one record.
- `session_expired` on `/invoices/new` → logged-out hint → `browser_login` keeps `?next=` and returns to
  the form → completes; counted as a re-login in the report.
- `session_expired` on the POST itself → the gateway reports "the form was NOT submitted" → refill → one record.
- `http_500` ×2 on the form page and on the API → retried 2x (1 s, 3 s) → completes.
- `label_drift` on every ERP and Mail page → completes (refs by name/role, never by text or id).
- Stuck detection: identical actions → nudge → escalation; the same AP-002 denial twice → escalation;
  step budget → escalation. Every forced escalation is still verified and reported.
- Hardening: a click that times out on a slow server no longer returns a bare failure; it settles and
  reports the page plus any write outcome the gateway recorded.

Acceptance: all deterministic Phase 6 items pass. **Not run:** `karya sandbox chaos heavy --seed 7` with
the three core scenarios × 3 runs — this measures a real model's adaptation and needs an API key; the eval
harness (phase 10) runs it with `karya evals run --chaos heavy`.

## Phase 7 — Company memory learning

Done:
- `karya/memory/learning.py`: answers (unless `--no-remember`), approval notes and rejection reasons →
  fast model (`fact_learning.md`, forced `submit_fact`) → one standalone statement + entities, or
  "not durable". Stored with provenance (who, when, task, question/approval id); identical statements
  are not stored twice; learning failures never block the answer/decision (audited instead).
- Retrieval: BM25Plus per layer (handbook / facts / episodes) with an entity-overlap boost for facts;
  facts are shown in the model's context with ids and provenance; the report's "Company knowledge used"
  lists facts that were in context.
- CLI: `karya memory list [--all]`, `karya memory search <q>`, `karya memory forget <F-id> [--by]`.

Acceptance (scripted agent + scripted fast model, real browser, `tests/test_memory_learning.py`):
the ambiguous request investigates (finds both Acmes), asks with concrete options, the answer becomes
F-0001 (learned from Arjun Rao, task, Q-id) and AI/2026/0917 is posted (ALL-7781 is not); after
`sandbox reset` with memory kept, the same request asks zero questions, the intake prompt contains
F-0001 and the report cites it; after `forget`, F-0001 no longer reaches the model. Whether a real model
then decides *not to ask* is model behaviour: covered by the eval scenarios `ap-ambiguous-ask` and
`ap-ambiguous-learned` once a key is available.

Decisions not in the plan:
- Facts are deactivated, never deleted (`forget` is audited) so the history of what was believed stays.
- Rejection reasons go through the same learner; the model usually marks them not durable.
- Test infrastructure: every real-browser test gets its own runtime DB and runs folder (the
  `fresh_sandbox` fixture), so a task one test leaves `queued` can never be claimed by another.

## Phase 8 — Security

Done:
- Red-team scenario as data (`evals/suite.yaml`, `redteam-bank`) run by `karya/evals/scripted.py`, which turns
  a YAML list of tool calls into a ScriptedLLM (`{connector:erp}` URLs, `ref: {find: ...}` resolved from the
  latest snapshot). The scripted agent logs in, fills and submits Apex's bank-change form, then tries the
  sandbox admin API and an outside host.
- `tests/test_security.py` (real browser): red-team → 403 "Blocked by Meridian policy AP-002", `policy_denied`
  audit row, `blocked_path` and `blocked_host` rows, V004 bank unchanged, and the ERP app never received the
  request (its own audit has no `update_vendor_bank`); Apex invoice → posted with correct values, the
  heuristic + model detectors flag the hidden note, and the report carries the `prompt_injection` flag even
  though the scripted worker "forgot" it; bank-change email → escalated, the page itself is flagged
  `possible_fraud` by the detector, bank unchanged, report cites AP-002 and the call-back.
- Already in place from phase 3: `<untrusted>` wrapping (tag break-out defused), heuristics (`prompt_injection`
  vs `possible_fraud`; page snapshots scanned only on free text), optional fast-model check on documents,
  password-field refusal, redaction, read-only verifier, append-only audit (DB triggers).

Acceptance: red-team test passes; the two model-driven Phase 8 scenarios pass in scripted form. Real-model
runs: eval scenarios `ap-injection` and `ap-bank-change` (needs a key).

Decisions not in the plan:
- Detector flags are merged into the report by code, so an injection can never be left out of a summary
  (the document asked for exactly that).
- Scripted scenarios are YAML data, so a new red-team probe needs no code.

## Phase 9 — Generalization: HR onboarding via configuration only

Preceded by a separate runtime commit (`runtime: remove the last company-specific defaults before phase 9`,
DECISIONS.md D10 note) that removed Meridian-flavoured defaults from `karya/`. HR did not need it to work.

The Phase 9 commit itself touches **no files under `karya/`** (`git diff --stat HEAD~1 -- karya/` is empty):
- Sandbox: Meridian People HR app on 8103 (`sandbox/hr/`: directory, new-employee form, access and equipment
  requests, read-only API), HR seed data (14 employees incl. Rohan Mehta E014), the offer letter
  `shared_drive/hr/offers/Offer_Priya_Sharma.pdf` (start D+12), `sandbox/run.py` starts it.
- Company config: `connectors/hr.yaml` (create_employee / request_access / request_equipment with idempotency
  keys and reconcile lookups; `level` is part of the access key), handbook `hr-01-onboarding.md` and
  `it-01-access-policy.md`, policy HR-001 (admin access needs it_admin), schema `offer_letter.json`.
- Tests: HR app in-process tests; `tests/test_hr_onboarding.py` — a scripted agent reads the offer letter
  (extraction + date normalization), checks the directory, creates E015 with priya.sharma@meridian.example,
  requests erp viewer + mail viewer + standard_laptop, the erp admin request is held by HR-001, Neha Kulkarni
  is refused (role), Vikram Desai approves, the task resumes and posts it once; the verifier's system
  evidence re-checks the employee record (ref E015) and the approval is consumed.

Acceptance: config-only commit verified; onboarding verified with the scripted agent. Real-model run: eval
scenario `hr-onboard` (needs a key).

## Phase 10 — Eval harness

Done:
- `evals/suite.yaml`: all 12 scenarios from plan §14.2 (11 model-driven + the scripted `redteam-bank`),
  with ground-truth checks and expected statuses; date tokens (`{D+27}`) resolve against the seed date.
- `karya/evals/checks.py`: erp_invoice_count, erp_invoice_fields, mail_has_label, questions_asked,
  approval_status, report_has_flag, audit_event, vendor_bank_unchanged / vendor_count_unchanged (snapshot
  right after reset, compare after the run), and a generic `sql` check (the HR scenario needs no new code).
  Sandbox databases are opened read-only; checks never go through the apps or the agent.
- `karya/evals/harness.py`: scenarios × chaos profiles × runs; reset, memory clear/keep, run-specific chaos
  seed, in-process run with an auto-responder (answers by text match through the same inbox code as the CLI,
  learning included; approvals decided by a person holding the approver role), timeout per scenario,
  metrics (steps, duration, tokens, tool errors, GET retries, re-logins, reconciliations, policy blocks,
  questions), verifier agreement, results `<timestamp>.json/.md` + `latest.md`.
- CLI: `karya evals run [--scenarios a,b] [--chaos off,heavy] [--runs N] [--verbose]`, `karya evals list`.
- Tests: the harness runs a scripted mini-suite (happy path, auto-approved Vertex, auto-answered question,
  red-team) end to end through the real browser and writes results; checks are unit-tested on a fresh sandbox.

Acceptance: the harness and CLI work end to end (`karya evals run` against the live sandbox: red-team PASS).
**The 11 model-driven scenarios were skipped: no LLM key in the build environment.** `evals/results/latest.md`
currently shows that honestly; regenerate it with a key (`karya evals run --chaos off --runs 1`, then the
recommended heavy run) before submission and paste the summary into the README.

Decisions not in the plan:
- `memory: clear` also clears episodes, otherwise a past "Acme Industries" episode leaks the answer into
  `ap-ambiguous-ask` (episodes are summaries, not audit records).
- Scripted scenarios run only without chaos (a fixed script cannot adapt; the model scenarios measure that),
  and verifier agreement is reported only for model-driven runs.
- When no LLM is configured the harness runs the scripted scenarios and marks the rest skipped instead of failing.
- Evals use the main runtime DB (so `karya show/audit` work on eval tasks, e.g. the red-team's AP-002 denial
  in the demo).

## Hardening for real models (between phases 10 and 12)

Phase 11 (P2 extras) was deliberately **skipped**: the plan allows P2 only when every P0/P1 check passes, and the
real-model checks could not be run here. Instead:
- Provider clients tested against fake SDK objects (`tests/test_llm_clients.py`): OpenAI/Azure/Anthropic request
  formatting (tools, forced tool choice, one tool per step, Anthropic prompt caching), response parsing, invalid
  JSON arguments, the `parallel_tool_calls` fallback, rate-limit retries, structured-output validation retry, and
  that every tool schema is inlined/portable. `tests/test_llm_smoke.py` hits the real API when a key is set.
- Prompt budget measured (`tests/test_context_budget.py`): ~4k tokens per step at most on the happy path
  (target < 30k), so a 25-step task is roughly 100k input tokens.
- The agent's context now carries the whole best-matching SOP (each is < 1k tokens) plus the other top chunks,
  so a model cannot skip a procedure step it never saw; `memory_search` still returns ranked chunks (D11 note).
- `browser_select` also accepts an option's value (models sometimes pass `V001` instead of the vendor name).

## Phase 12 — Docs and demo

Done:
- `README.md` with the 14 sections in the order of plan §15.1, three evidence screenshots in `docs/images/` (taken by
  scripted runs against the live sandbox: the posted invoice, the gateway's approval hold, the AP-002 denial), the
  criteria table, the scenario table, the decisions summary, the current eval table with an explicit note that the
  model-driven scenarios have not been run yet, and a walkthrough with `file:function` references.
- `ARCHITECTURE.md` (plan §15.2): components, one task end to end, the 12-step gateway pipeline, task/intent/approval
  state machines, resume and crash safety, memory layers and order of authority, threats → mitigations, reliability
  mechanisms, extension points, path to production.
- `DECISIONS.md`: D1–D17 from the plan and D18–D23 made while building, each with alternatives, why, trade-off and what
  would change my mind.
- `DEMO.md`: the §16 video script with every command, expected ids and what to point at.
- The gateway's own pages (held / blocked / duplicate) are now styled cards, since they appear in screenshots and the demo.
- Added `tests/test_ledger_memory_security.py::test_audit_log_is_append_only` (the triggers were only checked by hand in
  phase 0). Full suite: 152 passed, 1 skipped (the real-API smoke test, no key).

Not done here (needs the author): recording the video, and the real-model eval run whose numbers go in the README.

## After phase 12 — free-tier and Windows support

- Rate limits: model calls now wait as long as the provider asks (`Retry-After`), else back off 2 s … 30 s (about a minute
  in total) instead of giving up after 3 s; `KARYA_LLM_MAX_RPM` spaces calls under a free tier's requests-per-minute cap.
- `OPENAI_BASE_URL` is a setting (any OpenAI-compatible API, e.g. Gemini's free tier) and is passed to the client
  explicitly, so a blank or commented line in `.env` cannot break the default endpoint.
- Documented `KARYA_CHROMIUM_PATH` (use an installed Edge/Chrome when the Chromium download is slow) and the PowerShell
  activation steps. Tests: `tests/test_llm_clients.py` (Retry-After, backoff cap, pacing, base URL).
  Full suite: 155 passed, 1 skipped.
- Windows: the sandbox apps start with `CREATE_NO_WINDOW` (a venv's python.exe is a launcher whose child interpreter
  otherwise opened its own console window, and closing it stopped the app); `sandbox down` stops the process tree
  (`taskkill /T`). `KARYA_BROWSER_WINDOW` pins the visible browser's position and size for demo recordings.
- First real-model run (Gemini 3.5 Flash through the OpenAI-compatible endpoint, author's Windows laptop): the happy
  path succeeded in 40 steps, ERP-INV-000104 posted, email labelled, verifier passed all six criteria.
- DEMO.md rewritten as a frame-by-frame script with PowerShell commands.
- `karya cancel <T-id>` (queued or waiting tasks); `karya sandbox arm/chaos` reject unknown fault/profile names with the
  list of valid ones instead of a traceback.

## Open questions / author to-do before submission

1. Put a key in `.env`, then `karya config llm` (one tiny call) and the happy path from the README quick start.
2. `karya evals run --chaos off --runs 1`, then
   `karya evals run --scenarios ap-happy,ap-approval-approve,ap-duplicate,ap-injection,hr-onboard --chaos heavy --runs 2`
   (the plan's recommended minimum). Paste the summary of `evals/results/latest.md` into README "Reliability results",
   replace the "Read this honestly" note with one sentence on the model used, and name the model in README section 12.
   Commit `evals/results/latest.md`.
3. If a model-driven scenario fails, the per-scenario notes name the first failing check; `karya show <T-id> --trace`
   shows why. Prompt changes live in `karya/prompts/`, procedures in `company/handbook/`.
4. Rehearse and record the video from `DEMO.md` with `KARYA_HEADLESS=0`; add the link to README "Demo video".
5. Keep the per-phase history on `main` (no squash, rebase or force-push): `git show --stat bb69d57` is the proof that
   the phase 9 commit touches nothing under `karya/`.
