# ARCHITECTURE.md

Karya is a runtime for an AI employee: a person gives it a request in plain English, and it defines what "done"
means, works through real (sandbox) web apps one action at a time, pauses for people when policy or ambiguity
requires it, proves the outcome with an independent verifier, and reports back with evidence.

The design rests on three roles:

> **The AI decides. The gateway enforces. The verifier checks.**

The model chooses every action, but it never holds the authority. Every request it causes passes through
deterministic code that enforces company policy, and completion is judged by a second, read-only process that
looks at the systems of record, not at the worker's claims.

Contents: [Components](#1-components-and-responsibilities) ·
[One task, end to end](#2-data-flow-of-one-task) · [Gateway pipeline](#3-gateway-decision-pipeline) ·
[State machines](#4-state-machines) · [Resume and crash safety](#5-resume-semantics-and-crash-safety) ·
[Memory](#6-memory-layers-and-order-of-authority) · [Security model](#7-security-model) ·
[Reliability](#8-reliability-mechanisms) · [Extension points](#9-extension-points) ·
[Path to production](#10-path-to-production)

```
                    karya submit / karya run / eval harness
                                 │
                         ┌───────▼────────┐
                         │  Task queue    │  SQLite: tasks (status, lease, attempts)
                         └───────┬────────┘
                                 │ claim with lease (60 s, heartbeat 20 s)
┌────────────────────────────────▼──────────────────────────────────────────┐
│ Worker  (karya/worker/worker.py → karya/runtime/loop.py)                   │
│  1. Intake ── request → Goal Spec (success criteria) + plan                │
│         ▲ company memory: handbook chunks, learned facts, episodes         │
│  2. Loop ─── context → LLM → ONE tool call (+why) → observation            │
│              → ledger + checkpoint → replan / record facts → repeat        │
│  3. Verify ─ deterministic lookups + read-only verifier conversation       │
│  4. Report ─ report.md/json + screenshots + record refs + episode          │
└──────┬────────────────────────────────────────────────┬───────────────────┘
       │ tool calls                                       │ suspend on question / approval
┌──────▼─────────────────────────┐              ┌────────▼─────────────────┐
│ Tools                           │              │ Human inbox (CLI)        │
│ browser · api · files · memory  │              │ answer / approve / reject│
│ ask_human · update_plan · finish│              │ → learn → task requeued  │
└──────┬─────────────────────────┘              └──────────────────────────┘
       │ EVERY network request (browser routes + API) and file write
┌──────▼──────────────────────────────────────────────────────────────────┐
│ Gateway (enforcement)                                                    │
│ host allowlist → blocked paths → GET allow → match declared action →     │
│ mode check (read_only?) → payload → policy engine → duplicate check →    │
│ approval grants → intent log → send → record outcome → reconcile → audit │
└──────┬──────────────────────────────────────────────────────────────────┘
       ▼
  Sandbox apps (ERP 8101 · Mail 8102 · HR 8103) · shared drive
```

---

## 1. Components and responsibilities

| Component | Responsibility | Code |
| --- | --- | --- |
| **CLI** | All entry points: sandbox control, `run`/`submit`/`worker`, inbox (`answer`/`approve`/`reject`), `show`, `audit`, `memory`, `evals`, `config`. Streams each step with its `why`. | `karya/cli.py`, `karya/cli_display.py` |
| **Configuration** | Settings from `.env`; the company (profile, people and roles, handbook chunks, policies, connectors, schemas) loaded and cross-checked as one `Company` object. | `karya/config.py`, `karya/company.py`, `karya/connectors/` |
| **Runtime DB** | SQLite (WAL): tasks, append-only step ledger, task facts, write intents, approvals, questions, learned facts, episodes, append-only audit (DB triggers reject UPDATE/DELETE). | `karya/db.py` |
| **Queue + worker** | Exclusive claims with leases, heartbeats, crash reclaim, retries, suspend/requeue; `karya run` and `karya worker` share `process_task`. | `karya/worker/queue.py`, `karya/worker/worker.py` |
| **Intake** | Request → Goal Spec (objective, deliverable, checkable success criteria that name a system, constraints, assumptions, risks, blocking questions) + initial plan. | `karya/runtime/intake.py` |
| **Context builder** | Rebuilds each step's prompt from the ledger: system (instructions + company context, cacheable) + one user message (state, history, latest observation, budget). | `karya/runtime/context.py` |
| **Loop** | Guards → context → one model call → one tool call → observation → checkpoint; suspends for people; hands `finish` to the verifier. | `karya/runtime/loop.py` |
| **Ledger** | Append-only step log, mirrored to `runs/<task>/trace.jsonl`; rebuilds `TaskState` (history, plan, facts, decisions, answers, dangling step). | `karya/runtime/ledger.py` |
| **Tools** | Browser (Playwright, text snapshots with refs, screenshots, downloads, login with injected credentials), read-only APIs, files (sandboxed virtual roots, extraction to a schema), memory, ask_human, update_plan, finish, submit_verdicts. | `karya/tools/` |
| **Gateway** | The only path to the outside world: allowlist, declared actions, policy engine, payload-bound approvals, duplicate checks, write-intent log, reconciliation, audit. | `karya/gateway/` |
| **Verifier** | Code-made system evidence + an independent read-only model conversation; overall verdict computed in code. | `karya/runtime/verifier.py` |
| **Report** | `report.md`/`report.json`: what was done (with `why`), verification table, evidence, flags, knowledge used, stats; saves the episode. | `karya/runtime/report.py` |
| **Memory** | BM25 retrieval over handbook / learned facts / episodes; learning facts from human answers and decisions, with provenance; `forget`. | `karya/memory/` |
| **Security** | `<untrusted>` wrapping, injection and fraud heuristics, optional fast-model check, secret resolution and redaction. | `karya/security/`, `karya/gateway/secrets.py` |
| **LLM clients** | One protocol; OpenAI, Azure OpenAI and Anthropic clients; forced function calling for structured outputs; `ScriptedLLM` for tests. | `karya/llm/` |
| **Evals** | Scenario suite × chaos profiles × runs; auto-responder; ground-truth checks straight from the databases; results tables. | `karya/evals/`, `evals/suite.yaml` |
| **Sandbox** | Meridian Components: ERP, AP mailbox, HR portal (FastAPI + Jinja2 + SQLite), shared drive, seed data, generated PDFs, chaos middleware. Never imports `karya`. | `sandbox/` |

---

## 2. Data flow of one task

`karya run "Process the Vertex Semiconductors invoice in the AP mailbox."`, with the approval it needs:

```
 person        CLI / worker            loop + tools               gateway                  sandbox / DB
   │  run "…"      │                        │                         │                          │
   │──────────────▶│ enqueue T-0007 (queued)│                         │                          │
   │               │ claim (lease) ────────▶│ load_state(ledger)      │                          │
   │               │                        │ intake: retrieve SOP AP-01, facts, episodes        │
   │               │                        │ → Goal Spec c1..c3 + plan  (ledger: intake, plan)  │
   │               │                        │ step 1: browser_login('mail')  why: "…"            │
   │               │                        │──── GET/POST requests ─▶│ allowlist, auth action ─▶│ Mail
   │               │                        │ steps 2-9: open message, download PDF,             │
   │               │                        │   files_extract(invoice) → normalized values,      │
   │               │                        │   api_get(erp.search_vendors) → V003               │
   │               │                        │ step 10-15: open /invoices/new, fill, click Post   │
   │               │                        │──── POST /invoices ────▶│ policy AP-001 (> 50,000)│
   │               │                        │                         │ duplicate check: none ──▶│ ERP API
   │               │                        │◀── 202 Held for approval APR-0001 ───────────────│
   │               │ release: waiting_approval (lease freed)          │                          │
   │ approve APR-0001 --by "Neha Kulkarni"  │                         │                          │
   │──────────────▶│ role check, ledger: approval_decision, learn note, requeue                 │
   │               │ claim ────────────────▶│ reconcile in-flight intents; resume note           │
   │               │                        │ re-login, refill identical values, click Post      │
   │               │                        │──── POST /invoices ────▶│ approved scope matches   │
   │               │                        │                         │ intent #5 pending → send▶│ ERP
   │               │                        │                         │ 303 → lookup finds record│
   │               │                        │◀── committed ERP-INV-000104; APR-0001 consumed ───│
   │               │                        │ label email "Processed" (Mail write, allowed)      │
   │               │                        │ finish(completed) ─▶ verifier:                      │
   │               │                        │   system evidence (re-lookup, field compare, count) │
   │               │                        │   + read-only conversation → verdicts → overall    │
   │               │                        │ report.md/json, screenshots, episode               │
   │◀──────────────│ release: succeeded     │                         │                          │
```

What lands where:

- `steps` (ledger) and `runs/T-0007/trace.jsonl`: intake, plan, every model decision with its `why`, every observation,
  system notes (resume, nudges, verifier feedback), human answers, approval decisions, verification, report.
- `intents`: each write attempt with its payload, HTTP status, final status and record reference.
- `approvals`, `questions`: human-in-the-loop records; `memory_facts`: anything learned from them.
- `audit`: every gateway decision (allowed writes, denials, holds, duplicates, reconciliations, blocked hosts), human
  action, verification and worker event, append-only.
- `runs/T-0007/`: `screens/*.png` (after each action), `verify/screens/`, `downloads/`, `workspace/`, `report.md`,
  `report.json`.

---

## 3. Gateway decision pipeline

`karya/gateway/gateway.py`. `Gateway.decide()` is a pure function of the request, the database and an injectable
lookup, so all of it is unit-tested without a browser (`tests/test_gateway.py`). `on_route()` adapts it to Playwright;
`api_get()` and `check_file_write()` cover the other two channels.

| # | Step | Rule | Outcome when it fires |
| --- | --- | --- | --- |
| 1 | **Host allowlist** | Origin must belong to a connector (fail closed, D4). `data:`/`blob:`/`about:` pass. | Request aborted; `blocked_host` audited (sub-resources audited silently). |
| 2 | **Blocked paths** | `/__*` (sandbox admin) and connector `blocked_paths` globs. | 403 page; `blocked_path`. |
| 3 | **Reads** | GET/HEAD/OPTIONS on an allowed host. | Continue. |
| 4 | **Declared actions** | Any other method must match a declared action (method + path template, path params extracted). Auth actions (login/logout) pass without payload inspection. | 403 "undeclared write"; `unknown_write`. |
| 5 | **Mode** | The verifier's gateway is `read_only`. | 403; `read_only_block`. |
| 6 | **Payload** | Parse form/JSON/multipart; drop volatile fields (CSRF); add path params; coerce declared types (decimal, date, int). | Uncoercible values stay strings, so step 7 fails closed. |
| 7 | **Policy engine** | Structured conditions (`gt`, `eq`, `in`, …), AND within a rule; deny > require_approval > allow; a condition that can't be evaluated counts as matching. | Deny → 403 "Blocked by Meridian policy AP-002: …"; `policy_denied`. |
| 8 | **Duplicates** | Idempotency key (e.g. `vendor_id + invoice_number`): earlier intents in this task (in flight → refuse; pending/unknown → reconcile first; committed → refuse), then a pre-write lookup in the system of record (D7). Runs **before** the approval hold (D19). | 409 "Already done in this task (ERP-INV-000104)" / "A record with the same … already exists"; lookup failure → 503, not sent. |
| 9 | **Approvals** | Rule requires approval → scope hash over connector, action, idempotency fields and rule-referenced fields (D5). Approved scope → send (and remember the approval id); rejected scope → 403; otherwise create/reuse an `APR-` row. | 202 "Held for approval APR-0001 …"; task suspends. |
| 10 | **Intent log, send** | Intent row `pending` before the request leaves; in-flight set; `route.fetch(max_redirects=0)`. | — |
| 11 | **Classify + reconcile** | 3xx to the logged-out path → `failed` ("NOT submitted"); 2xx/3xx → `committed` only if the reconcile lookup finds the record (and yields its ref); 4xx → `failed`; 5xx/timeout → `unknown` → reconcile now (D6). Approval consumed on commit. | Message to the agent states exactly what happened and whether resubmitting is safe. |
| 12 | **Audit** | Every decision above writes an audit row (`actor=gateway`) with payload, rule ids, intent and approval ids. | — |

An exception anywhere in `decide()` produces a 500 page and the request is **not** sent (D22). The pages the gateway
returns are styled HTML cards ("Answered by the Karya gateway, not by the company app"), so they read clearly in the
task's screenshots and in the snapshot the model sees.

---

## 4. State machines

### Task status (`tasks.status`)

```
submit ─▶ queued ──claim──▶ running ──finish + verify (≤ 2 rounds)──▶ succeeded | partial | escalated | failed
            ▲  ▲             │  │  │
            │  │             │  │  └─ Ctrl-C, lease lost, crash with attempts < 3 ─────▶ queued
            │  │             │  └──── 202 approval required ─▶ waiting_approval ──┐
            │  │             └─────── ask_human ─────────────▶ waiting_human ──┐  │
            │  └──────────────────────── karya answer ─────────────────────────┘  │
            └─────────────────────────── karya approve / reject ──────────────────┘
```

- `finish(completed)` + overall pass → `succeeded`; `finish(partial)` → `partial`; `finish(escalated)` → `escalated`;
  `finish(cannot_complete)` → `failed`. A completion the verifier rejects twice ends as `partial` (some criteria pass)
  or `failed`.
- Budgets (steps, tokens, active minutes) and stuck detection force `finish(escalated)`, which is still verified and
  reported.
- A task whose worker crashed three times is marked `failed` with the evidence kept. `cancelled` is reserved (terminal;
  no CLI command yet).

### Write intent (`intents.status`)

```
          created before the request is sent
                       │
                       ▼
                    pending ──2xx/3xx and the record is found──────────────▶ committed
                       │    ──4xx · 3xx to /login · 2xx but no record──────▶ failed
                       │    ──5xx · timeout · reset · worker died mid-write─▶ unknown
                                                                               │
                                       reconcile lookup: record exists ────────┼──▶ committed (reconciled)
                                                         no record ────────────┼──▶ failed    (reconciled)
                                                         lookup failed ────────┴──▶ stays unknown:
                                                                                    resubmission blocked
```

Reconciled outcomes are final; a late response cannot overwrite them.

### Approval (`approvals.status`)

```
  202 held ─▶ pending ──approve (holder of the rule's role)──▶ approved ──write commits──▶ consumed
                 │
                 └──────reject (holder of the role, reason required)──▶ rejected
                                                                        (identical payload refused; task reports it)
```

The same pending approval is reused if the agent resubmits the identical payload while waiting. A changed payload
(different amount, vendor, …) has a different scope hash and needs its own approval.

---

## 5. Resume semantics and crash safety

The task's state is the ledger, not the process. Everything needed to continue lives in SQLite:

1. **Every step is checkpointed twice.** The model's decision (`llm` entry: tool, arguments, `why`) is appended before the
   tool runs; the `observation` after. A crash between the two leaves a *dangling* step, which `load_state` detects; the
   resume note says "the previous run stopped during step 14 (browser_click); re-check the page before repeating it."
2. **Writes are settled before anything else.** On resume, `Gateway.reconcile_unresolved()` marks `pending` intents
   `unknown` ("worker stopped mid-write") and reconciles every unknown intent against the system of record. The agent then
   reads "the record exists (ERP-INV-000104). The write succeeded; do not resubmit." and, if it resubmits anyway, the
   duplicate check refuses.
3. **Browser state is disposable.** A resumed task gets a fresh browser; the resume note says it is not logged in.
   Downloads and workspace files persist in `runs/<task>/`.
4. **Leases.** A claim is a 60 s lease renewed every 20 s by a heartbeat thread. `kill -9` stops the heartbeat; once the
   lease expires another worker reclaims the task (`task_reclaimed` audit) and resumes it. If a worker loses its lease, it
   aborts the task after the current step instead of racing the new owner.
5. **Suspension holds nothing.** `waiting_human` / `waiting_approval` release the lease. The decision is appended to the
   ledger and the task requeued, so a task can wait three days and resume on any worker.
6. **Shutdown.** First Ctrl-C: finish the current step, checkpoint, release (task back to `queued`). Second Ctrl-C: exit now
   (the lease expires and the task is reclaimed).
7. **Retries are for crashes, not for waiting.** `attempts` counts failed runs only; a clean suspension or finish resets it.

All of this is exercised with a real browser in `tests/test_human_in_loop.py` (simulated `kill -9` after a write → reclaim →
exactly one ERP record) and `tests/test_worker_signals.py` (SIGINT in a real subprocess).

---

## 6. Memory layers and order of authority

| Layer | Source | Lifetime | How it reaches the model |
| --- | --- | --- | --- |
| **Policies** | `company/policies.yaml`, authored | Config | Listed in the company context, and **enforced by the gateway** whatever the model believes |
| **Handbook** | `company/handbook/*.md`, authored SOPs chunked on `##` | Config | BM25 top chunks; the best-matching SOP in full (D11 note); `memory_search` |
| **Learned facts** | Human answers, approval notes, rejection reasons → fast model → one standalone statement + entities | Until forgotten (deactivated, audited) | BM25 with an entity boost, shown with id and provenance ("learned from Arjun Rao on 2026-10-08, T-0003, Q-0001") |
| **Episodes** | One summary per finished task | Until cleared | "Similar past tasks" hints |
| **Task facts** | `record_fact`, extracted document values | One task | Task state block of every step |

**Order of authority:** policies > handbook > learned facts > episodes. A learned fact can resolve an ambiguity ("'Acme'
in AP requests means Acme Industries, V001") but never relax a policy. Content from pages, emails and documents has **no**
authority: it is wrapped in `<untrusted>` tags and is data only.

Learning (`karya/memory/learning.py`) runs when a person answers or decides; it is optional per answer
(`--no-remember`), deduplicated, and never blocks the human action (failures are audited). The report lists which facts
were in the model's context ("Company knowledge used"), so a person can trace and `karya memory forget` a wrong one.

---

## 7. Security model

The model is treated as an untrusted component: it may be confused, wrong, or manipulated by content it reads. Safety
comes from the layer it cannot change.

| Threat | Mitigations | Verified by |
| --- | --- | --- |
| **Prompt injection** in documents, emails, pages (e.g. hidden 6pt text on the Apex invoice) | `<untrusted>` wrapping with tag break-out defused; system prompt rule; heuristic detector + optional fast-model check → flags shown as trusted warnings; detector flags merged into the report by code (D20); the gateway enforces regardless | `tests/test_security.py`, eval `ap-injection` |
| **Fraud via bank change** (social engineering email) | Policy AP-002 `deny` in the gateway; `possible_fraud` detector; handbook AP-03 (call back on the number already on file, escalate) and SEC-01 (red flags); stuck detection escalates a repeated denial | Red-team scenario `redteam-bank` (403 AP-002, the ERP never receives the request), eval `ap-bank-change` |
| **Credential leakage** | Connector specs name env vars, never values; `browser_login` fills credentials itself; `browser_fill` refuses password fields; API keys are added as headers by the gateway; every observation is redacted before it is logged or shown | `tests/test_ledger_memory_security.py`, `tests/test_gateway.py` |
| **Overreach to other hosts or functions** | Host allowlist (fail closed); declared write actions only; `/__*` admin paths blocked; file writes only to the task workspace or a connector's write globs; AP-003 denies vendor creation | Red-team (admin API, outside host), `tests/test_gateway.py`, `tests/test_files.py`, eval `ap-unknown-vendor` |
| **Duplicate payments** | In-task intent check, in-flight guard, pre-write existence lookup, reconciliation of unknown outcomes, no blind write retries | `tests/test_reliability.py`, eval `ap-duplicate` |
| **False completion claims** | Goal Spec criteria up front; independent read-only verifier; deterministic re-lookup and field comparison; overall verdict computed in code; eval checks independent of the agent | `tests/test_e2e_scripted.py` (corrupted record → fail → `partial`), eval verifier agreement |
| **Approval abuse** (approve one thing, post another; reuse an approval) | Scope hash over the payload fields that matter; single use; approver must hold the rule's role | `tests/test_human_in_loop.py`, `tests/test_gateway.py` |
| **Verifier side effects** | Separate browser context; gateway in `read_only` mode (its browser may click to navigate, but no write request leaves it); its tool set has no file writes, extraction, facts, questions or `finish` | `tests/test_gateway.py`, `tests/test_llm_clients.py` |
| **Tampering with the record** | `audit` is append-only (SQLite triggers abort UPDATE/DELETE); ledger append-only; facts deactivated, never deleted | `tests/test_ledger_memory_security.py::test_audit_log_is_append_only` |
| **Runaway agent** (loops, cost) | Step, token and active-time budgets; repeat detection (nudge, then escalate); five consecutive failures → escalate | `tests/test_reliability.py` |
| **Approver impersonation** | **Not mitigated**: the CLI trusts `--by`. Production needs SSO-backed approvals (see §10) | Known limitation |

---

## 8. Reliability mechanisms

| Unexpected state | Response |
| --- | --- |
| HTTP 500 / slow page on a read | Browser GETs and API reads retried twice (1 s, 3 s); timeouts settle and report the page instead of failing blindly |
| Session expired | Logged-out redirect detected; the observation tells the agent to `browser_login`, which keeps `?next=` so the agent lands back on the form; a POST that bounced to `/login` is reported as "NOT submitted" |
| Changed labels / ids / field order (`label_drift`) | Fresh refs every snapshot, elements described by visible label and field name; no stored selectors |
| Stale ref | Clear error with a fresh snapshot, never a guessed click |
| Write returned 5xx/timeout but committed (`commit_then_504`) | Intent `unknown` → reconcile → `committed` with the record ref; resubmission refused |
| Write rejected (`write_conflict_409`) | Intent `failed`, "nothing was saved", safe to resubmit |
| Worker crash / kill | Lease expiry → reclaim → reconcile in-flight writes → resume from the ledger |
| Model replies without a tool call, invalid arguments | Note to the model; three no-tool replies → escalate; validation errors returned as observations |
| Model stuck | Repeat detection → nudge → escalate; budgets → escalate (always verified and reported) |
| Provider rate limits / transient errors | Client retries with backoff (`with_retries`); one validation retry for structured outputs |

Chaos profiles (`karya sandbox chaos light|heavy --seed N`) and armed faults (`karya sandbox arm commit_then_504 --count 1`)
make each of these reproducible; `tests/test_reliability.py` covers them deterministically with real Chromium.

---

## 9. Extension points

The likely "change it live" requests, and exactly what to touch. Run `karya config check` after any config change: it
validates cross-references (rule → connector/action/field, approver role held by someone, reconcile endpoint declared).

| Change | What to edit | Code changes |
| --- | --- | --- |
| **New policy** (e.g. "invoices from Bharat Packaging above INR 20,000 need approval") | Add a rule to `company/policies.yaml`: `match: {connector: erp, action: create_invoice}`, `when: [{field: vendor_id, op: eq, value: V006}, {field: amount, op: gt, value: 20000}]`, `effect: require_approval`, `approver_role: finance_manager` | None |
| **New connector** (another web app) | `company/connectors/<id>.yaml`: `base_url`, `auth` (login path, credential env var names, logged-out signal), `actions` (method, path template, typed fields, `idempotency_key`, `reconcile` lookup, `display`), optional read `api`; credentials in `.env` | None |
| **New procedure** | `company/handbook/<id>.md`, one `##` section per step | None |
| **New document schema** | `company/schemas/<name>.json`: fields with types, `required`, `checks` (`sum`, `date_order`) | None |
| **New tool** | One `Tool` subclass in `karya/tools/` (name, description, pydantic `Args` including `why`, `roles`, `run()` returning an `Observation`) + one line in `karya/tools/registry.py:ALL_TOOLS`. Any network or file effect must go through `session.gateway`. | One class + one line |
| **New LLM provider** | One client class implementing `chat(messages, tools, tool_choice, model) -> LLMResponse` + one branch in `karya/llm/factory.py:build_llm` | One class + one branch |
| **New company** | Copy `company/` to a new directory and set `KARYA_COMPANY_DIR` | None |
| **New eval scenario** | An entry in `evals/suite.yaml` (request, human script, expected status, checks; the generic `sql` check covers new apps) | None |
| **New chaos fault** | Add it to `FAULTS` and the middleware in `sandbox/common/chaos.py` | Sandbox only |

The proof that this works: HR onboarding (new app, two SOPs, policy HR-001, connector, schema) was added in commit
`bb69d57`, which touches no file under `karya/` (`git show --stat bb69d57`).

---

## 10. Path to production

What changes between this prototype and a system a company runs its back office on. The interfaces stay; the
implementations behind them scale.

1. **Durable state and queue.** Move tasks, ledger, intents and approvals to Postgres. `claim()` becomes
   `SELECT … FOR UPDATE SKIP LOCKED` with the same lease/heartbeat semantics; or run each task as a durable workflow
   (Temporal), with each step an activity, approvals as signals and budgets as timers. The ledger-first design (D18)
   already makes every step replayable.
2. **Browser fleet.** One isolated, disposable browser per task in containers (Playwright server or a managed grid),
   scheduled next to workers; downloads and screenshots in object storage; egress restricted at the network level to
   the connector allowlist as defence in depth behind the gateway.
3. **Per-tenant isolation.** Separate company config, a secrets vault per tenant (credentials resolved at the gateway,
   never in prompts), data partitions or separate databases, per-tenant model keys and budgets.
4. **Approvals where people work.** Slack/Teams approval cards with SSO identity (closing the approver-impersonation gap),
   approval chains and delegation, expiry and reminders, all still bound to the payload scope hash.
5. **Observability.** OpenTelemetry traces (task → step → tool → gateway decision → HTTP) and metrics (success rate, steps,
   tokens, reconciliations, policy blocks per workflow); the audit table exported to the company's SIEM.
6. **Eval-gated releases.** Every prompt, model or runtime change runs the scenario suite (with chaos); no deploy if the pass
   rate or verifier agreement drops. Continuous evaluation on sampled production traces with human-labelled outcomes.
7. **Cost controls.** Smaller/faster models for routine steps and extraction, the strong model for planning and recovery;
   prompt caching (the system block is already stable between re-retrievals); caching retrieval and unchanged snapshots;
   per-task and per-tenant budgets (already enforced per task).
8. **Desktop applications.** OS accessibility APIs (UI Automation on Windows, AX on macOS) inside disposable VMs, exposed
   as tools with the same snapshot-with-refs contract, with the gateway in front of the actions those tools perform.
9. **Vision fallback.** Send the screenshot when the text snapshot is too thin (canvas UIs) or refs keep going stale.
10. **Sub-task fan-out.** Batch tasks spawn one child per item so one approval doesn't block the rest; the parent
    aggregates and verifies.
11. **Learned procedures.** Turn successful traces into draft SOPs for human review, and propose connector specs by
    exploring an app, always approved by a person before they take effect.
