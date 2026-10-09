# DECISIONS.md

Each entry: **Decision · Alternatives considered · Why · Trade-off · What would change my mind.**
Code comments point here as `# see DECISIONS.md Dn`. D1–D17 are the decisions the plan called for;
D18–D23 are decisions made while building.

| ID | Decision |
| --- | --- |
| [D1](#d1-hand-rolled-loop-instead-of-langgraphcrewaibrowser-use) | Hand-rolled loop instead of LangGraph/CrewAI/browser-use |
| [D2](#d2-text-snapshots-with-element-refs-instead-of-pixelvision-control-screenshots-as-evidence) | Text snapshots with element refs; screenshots as evidence |
| [D3](#d3-enforcement-in-a-network-level-gateway-rather-than-in-prompts) | Enforcement in a network-level gateway rather than in prompts |
| [D4](#d4-fail-closed-allowlist-of-hosts-and-declared-write-actions) | Fail-closed allowlist of hosts and declared write actions |
| [D5](#d5-approvals-bound-to-a-payload-scope-hash-and-single-use) | Approvals bound to a payload scope hash, single-use |
| [D6](#d6-write-intent-log--reconciliation-for-unknown-outcomes-never-blind-write-retries) | Write-intent log + reconciliation; never blind write retries |
| [D7](#d7-pre-write-existence-check-as-a-deterministic-duplicate-backstop) | Pre-write existence check as a duplicate backstop |
| [D8](#d8-goal-spec-with-success-criteria-before-acting) | Goal Spec with success criteria before acting |
| [D9](#d9-independent-read-only-verifier--deterministic-lookups) | Independent read-only verifier + deterministic lookups |
| [D10](#d10-company-specific-config-separated-from-the-runtime-company) | Company config separated from the runtime |
| [D11](#d11-memory-authored-handbook-vs-learned-facts-with-provenance-humans-can-forget-facts) | Memory layers; learned facts with provenance; forget |
| [D12](#d12-sqlite-queue-with-leases-and-how-it-maps-to-production) | SQLite queue with leases |
| [D13](#d13-one-tool-call-per-step-mandatory-why) | One tool call per step, mandatory `why` |
| [D14](#d14-read-apis-for-lookups-browser-for-ui-only-actions-tool-selection) | Read APIs for lookups, browser for UI actions |
| [D15](#d15-sandbox-company-with-chaos-instead-of-real-sites-brief-compliance-reproducible-evals) | Sandbox company with chaos instead of real sites |
| [D16](#d16-money-as-integer-paise-deterministic-normalization-after-llm-extraction) | Money as integer paise; deterministic normalization |
| [D17](#d17-forced-function-calling-for-structured-outputs-portable-across-providers) | Forced function calling for structured outputs |
| [D18](#d18-stateless-steps-rebuilt-from-the-ledger) | Stateless steps rebuilt from the ledger |
| [D19](#d19-duplicate-check-before-approval-holds-the-system-of-record-decides-committed) | Duplicate check before approval; system of record decides "committed" |
| [D20](#d20-reports-merge-code-made-facts-not-only-the-models-summary) | Reports merge code-made facts |
| [D21](#d21-scripted-agents-as-data-for-tests-and-red-team-probes) | Scripted agents as data |
| [D22](#d22-fail-closed-on-uncertainty-inside-the-gateway) | Fail closed on uncertainty inside the gateway |
| [D23](#d23-evaluation-design-ground-truth-from-the-databases-not-from-the-agent) | Evaluation design |

---

## D1 Hand-rolled loop instead of LangGraph/CrewAI/browser-use

- **Decision.** The agent loop is ~230 lines of plain Python (`karya/runtime/loop.py:run_task`): guard →
  build context → one model call → one tool call → observation → ledger checkpoint → repeat.
- **Alternatives.** LangGraph (graph + checkpointer), CrewAI/AutoGen (multi-agent roles), browser-use /
  Stagehand / Skyvern (browser agents with their own loop and DOM handling).
- **Why.** The interesting parts of this system are exactly the parts frameworks hide or make awkward:
  suspending a task for a human and resuming it days later in another process, rebuilding context from a
  durable ledger, routing *every* request through an enforcement layer, and handing the finish over to an
  independent verifier. With a hand-rolled loop each of those is a visible line of code I can explain,
  debug and change live. Browser agents also own the network and the DOM, which is precisely where the
  gateway must sit.
- **Trade-off.** I wrote my own checkpointing, retries, streaming display and tool registry; no ecosystem
  of prebuilt integrations.
- **What would change my mind.** Many heterogeneous, long-lived workflows maintained by a team that already
  knows LangGraph, or a need for features (visual graph debugging, managed persistence) that would cost more
  to build than to adopt. Even then the gateway and verifier would stay outside the framework.

## D2 Text snapshots with element refs instead of pixel/vision control; screenshots as evidence

- **Decision.** The model sees a compact text snapshot of the page (`karya/tools/snapshot.js`): headings,
  text, tables and interactive elements tagged `[eN]` (`data-kref`), roughly 1–4k tokens. It acts by ref
  (`browser_click(ref="e12")`). Full-page screenshots are saved after each action as evidence for humans and
  the report, not sent to the model.
- **Alternatives.** Vision models clicking coordinates on screenshots; raw HTML/DOM dumps; accessibility tree
  only.
- **Why.** Text is cheaper (~4k tokens per step measured on the happy path, `tests/test_context_budget.py`),
  deterministic to test, and robust to cosmetic change: refs are assigned fresh on every snapshot, each element
  is described by its kind, visible label and field name, and the model acts on what the page says now rather than
  on stored selectors, so the chaos `label_drift` fault ("Post invoice" becoming "Submit entry", shuffled ids and
  field order) does not break runs. Screenshots still give a reviewer a visual record of every step.
- **Trade-off.** Canvas-heavy or highly dynamic UIs (charts, drag-and-drop, remote desktops) cannot be read
  as text. Refs are reassigned on each snapshot, so the model must use the latest one (the runtime reports
  stale refs clearly instead of guessing).
- **What would change my mind.** Target apps that are canvas/remote-desktop based. The plan then is a vision
  fallback (send the screenshot when the snapshot has too few interactive elements or after repeated stale
  refs), behind the same tool interface.

## D3 Enforcement in a network-level gateway rather than in prompts

- **Decision.** Every browser request goes through `context.route("**/*", gateway.on_route)` registered before
  the first navigation; API calls go through `Gateway.api_get`; file writes through `Gateway.check_file_write`.
  Policies are evaluated in code (`karya/gateway/gateway.py:Gateway.decide`).
- **Alternatives.** Policies in the system prompt only; a tool-level check inside each tool; a separate HTTP
  proxy process.
- **Why.** Prompts advise; code enforces. A model that is confused, injected, or simply wrong can type into any
  form and click any button, but the request it produces still has to pass the gateway. The red-team scenario
  proves it: a scripted agent fills and submits Apex's bank-change form, the gateway answers 403 "Blocked by
  Meridian policy AP-002", and the ERP's own audit shows the request never arrived. Intercepting at the network
  layer also covers writes the tool layer can't see (a form submitted by pressing Enter, a JS-initiated POST).
- **Trade-off.** Only HTTP-visible intent is enforceable: the gateway sees "POST /vendors/V004/bank", not "the
  model wants to commit fraud". Each write action must be declared in the connector spec with its fields.
- **What would change my mind.** Channels that cannot be intercepted (native desktop apps, binary protocols).
  There the same gateway interface would sit in front of the tool that performs the action (e.g. an
  accessibility-API click that submits a dialog).

## D4 Fail-closed allowlist of hosts and declared write actions

- **Decision.** Only origins declared by a connector are reachable; any non-GET request must match a declared
  action (method + path template); `/__*` paths (sandbox admin) and connector `blocked_paths` are refused.
  Everything else is blocked by default.
- **Alternatives.** Deny-list of dangerous hosts/actions; allow all reads and writes to known hosts.
- **Why.** An agent operating real systems must not discover new capabilities by accident. Default-deny turns
  every new capability into an explicit, reviewable YAML change. Unknown origins are aborted (navigation and
  sub-resources), undeclared writes get a 403 page explaining why, and the attempt is audited.
- **Trade-off.** Adding a workflow means declaring its write actions first; a mis-declared path template blocks
  a legitimate action (loudly, which is the right failure).
- **What would change my mind.** Nothing for writes. For reads, a research-style agent that must browse the open
  web would need a separate, read-only browsing mode without credentials.

## D5 Approvals bound to a payload scope hash and single-use

- **Decision.** When a rule requires approval, the gateway computes a `scope_hash` over the connector, the action
  and the fields that matter (the idempotency key plus every field the rule's conditions reference), creates an
  `APR-` row and answers 202 "Held for approval". An approval lets through only a request with the same scope,
  only once (`consumed` after the write commits). The approver must hold the rule's role (Arjun Rao is refused
  for AP-001; Neha Kulkarni, finance_manager, can approve).
- **Alternatives.** Approve a task ("go ahead with T-0007"); approve an action type; time-boxed approvals.
- **Why.** An approver approves *a specific thing*: "Invoice VS-55120, INR 1,84,500". If the model later changed
  the amount or the vendor, a task-level approval would silently cover it. Binding to the payload makes the human
  decision mean exactly what the human saw. Single use prevents one approval from covering a second post.
- **Trade-off.** Any correction to an approved payload needs a new approval; volatile fields (CSRF tokens) must be
  declared so they don't change the hash.
- **What would change my mind.** Approval chains or standing approvals ("auto-approve this vendor up to INR 1L this
  month") would extend the model, but each grant would still be bound to an explicit scope.

## D6 Write-intent log + reconciliation for unknown outcomes; never blind write retries

- **Decision.** Before a write is sent, an intent row is created (`pending`). The response classifies it:
  `committed` / `failed` / `unknown` (5xx, timeout, connection reset). An `unknown` write is reconciled by looking
  the record up in the system of record (the connector's `reconcile` spec): exists → `committed`, absent →
  `failed` (safe to retry), lookup failed → stays `unknown` and blocks resubmission. Reconciled outcomes are
  final; late responses cannot overwrite them. On resume, `pending` intents (the worker died mid-write) are
  reconciled before the first new step.
- **Alternatives.** Retry writes on error; ask the model to check; rely on the app's own duplicate detection.
- **Why.** The UPI-pending problem: "did my payment go through?" A 504 does not mean the write failed. The chaos
  fault `commit_then_504` commits the invoice and then returns 504; the gateway tells the agent "Meridian ERP
  returned HTTP 504, but the record exists (ERP-INV-000104). The write succeeded; do not resubmit", and a model
  that resubmits anyway is blocked ("Already done in this task").
- **Trade-off.** Every reconcilable write type needs a lookup (an API endpoint + parameters + an exists rule) in its
  connector spec. Writes without one are reported as unknown and left to a human.
- **What would change my mind.** Systems with server-side idempotency keys (Stripe-style `Idempotency-Key`): then
  retrying with the same key is safe and reconciliation becomes a fallback.

## D7 Pre-write existence check as a deterministic duplicate backstop

- **Decision.** For writes with an idempotency key and a reconcile spec, the gateway checks earlier intents in the
  task and then asks the system of record whether the record already exists, *before* sending. If it exists, the
  write is refused with 409 "A record with the same vendor_id and invoice_number already exists (ERP-INV-000102)".
  If the lookup itself fails, the write is not sent (503, fail closed).
- **Alternatives.** Trust the model to follow handbook step AP-01 #5 (check for duplicates); a database uniqueness
  constraint in the target app.
- **Why.** Paying the same invoice twice is the most expensive realistic mistake in AP. The handbook tells the model
  to check, and it usually does; the gateway guarantees it. The sandbox ERP deliberately has no uniqueness
  constraint, as many real systems don't.
- **Trade-off.** One extra read per write; a slow lookup API slows writes; the key must be chosen well (vendor_id +
  invoice_number, not the amount).
- **What would change my mind.** If the system of record enforces uniqueness itself and returns a clear error, the
  backstop becomes redundant (but cheap enough to keep).

## D8 Goal Spec with success criteria before acting

- **Decision.** Intake (`karya/runtime/intake.py:run`) turns the request into a Goal Spec: objective, deliverable,
  checkable success criteria (each naming the system where it is checked), constraints, assumptions, risk notes,
  blocking questions, and an initial plan. Criteria that name no system are sent back for correction once.
- **Alternatives.** Start acting immediately and decide "done" at the end; free-text plan only.
- **Why.** "Done" defined after the fact is whatever the agent managed to do. Criteria written first are what the
  independent verifier checks, and they keep the loop pointed at the user's actual objective (the invoice is in the
  ERP *and* the email is labelled Processed). Blocking questions let intake ask a human before the first action when
  investigation cannot resolve an ambiguity.
- **Trade-off.** One extra model call per task; a bad criterion propagates (mitigated by the system-name check and by
  deterministic evidence the verifier gets regardless of criteria).
- **What would change my mind.** Purely exploratory tasks ("look around and tell me what's odd") where criteria can't
  be stated up front; those would use a lighter spec with a deliverable only.

## D9 Independent read-only verifier + deterministic lookups

- **Decision.** When the worker calls `finish`, `karya/runtime/verifier.py:verify` (1) builds system evidence in code:
  every committed write is looked up again and compared field by field with what was submitted, and matching records
  are counted (duplicates show up); (2) runs a separate model conversation that never sees the worker's transcript,
  only criteria, claims, facts, the intent log and the evidence, with its own browser context and the gateway in
  `read_only` mode; (3) computes the overall verdict in code: pass only if every criterion passes and the evidence
  shows no mismatch. A failed verification is fed back to the worker once; the second failure ends the task as
  `partial` or `failed`.
- **Alternatives.** Trust the worker's `finish`; self-critique in the same conversation; deterministic checks only.
- **Why.** Never trust self-reports. The same conversation that made a mistake tends to confirm it. Deterministic
  lookups catch the expensive errors (wrong amount, missing record, duplicate) with certainty; the model verifier
  covers criteria that are hard to code (labels, messages, files). The eval harness measures how often the verifier
  agrees with ground truth.
- **Trade-off.** Extra tokens and time per task (bounded to 15 verifier steps); the verifier uses the same model family
  as the worker, so some errors are correlated (the deterministic evidence is the mitigation).
- **What would change my mind.** If measured verifier agreement were consistently ~100% on deterministic checks alone,
  the model verifier would be reserved for criteria without a lookup.

## D10 Company-specific config separated from the runtime (`company/`)

- **Decision.** Apps (connectors), procedures (handbook), rules (policies), document schemas and people live in
  `company/` as YAML/Markdown/JSON. `karya/` contains no company knowledge.
- **Alternatives.** Hard-code the AP workflow; per-workflow Python plugins.
- **Why.** Generalization is a grading criterion and a product requirement: a new workflow should be a configuration
  change reviewed by the business, not a code change. The proof is the Phase 9 commit (`bb69d57`): HR onboarding
  (a new app, two SOPs, a policy, a connector, a schema) with `git show --stat` listing no file under `karya/`.
- **Trade-off.** The connector schema must be expressive enough (path templates, idempotency keys, reconcile lookups,
  volatile fields); workflows needing new *capabilities* (a new tool) still need code.
- **What would change my mind.** If most new workflows needed bespoke logic, a plugin interface (still outside `karya/`)
  would be cleaner than ever-richer YAML.

_Pre-phase-9 note:_ before adding HR onboarding as a configuration-only change, the last Meridian-flavoured defaults
were removed from `karya/` in a separate commit (`fc33777`): `files_extract` no longer defaults to the `invoice` schema
(the model must name a schema from the company context), an error message no longer cites an ERP URL, and the system
prompt's document tip is generic. None of these was needed for HR to work; they were removed so that `karya/` contains
no company knowledge at all.

## D11 Memory: authored handbook vs learned facts with provenance; humans can forget facts

- **Decision.** Three layers, retrieved separately: the authored handbook (SOP chunks), learned facts (from human
  answers, approval notes and rejection reasons, each with who/when/task/question-or-approval id), and episodes (one
  summary per finished task). Order of authority: policies (enforced) > handbook > learned facts > episodes. Facts are
  learned by a fast model through a forced `submit_fact` call, which may answer "not durable". `karya memory forget`
  deactivates a fact (audited; never deleted).
- **Alternatives.** One vector store for everything; fine-tuning; no learning (ask every time).
- **Why.** The second "Enter the pending Acme invoice" should not ask again, but a learned fact must never be confused
  with company policy, and a wrong answer must be traceable and removable. Provenance makes every fact explainable
  ("learned from Arjun Rao, T-0003, Q-0001"), and the report lists which facts were in context.
- **Trade-off.** A human's wrong answer becomes a wrong fact until someone forgets it; BM25 retrieval misses synonyms.
- **What would change my mind.** Large handbooks where lexical retrieval misses; then embeddings (hybrid with BM25),
  still per layer and still with provenance.

_Retrieval note:_ BM25Plus (not BM25Okapi) because Okapi's IDF collapses on tiny corpora (one learned fact). For the
agent's context, the best-matching handbook document is included in full (an SOP is under 1k tokens), followed by the
other top chunks: a model that sees only fragments of a procedure can skip a step. `memory_search` still returns ranked
chunks.

## D12 SQLite queue with leases (and how it maps to production)

- **Decision.** Tasks live in SQLite (WAL). `claim()` runs inside `BEGIN IMMEDIATE`, takes the oldest runnable task and
  sets a 60 s lease renewed every 20 s by a heartbeat thread. A crashed worker's task is reclaimed when the lease expires
  and resumes from its ledger. `attempts` counts failed runs only (a suspension or clean finish resets it); three
  crashes → `failed`.
- **Alternatives.** Redis/RQ/Celery; Postgres; Temporal.
- **Why.** Zero infrastructure for a reviewer (`pip install -e .` and go) while keeping the semantics that matter: exclusive
  claims, leases, crash recovery, suspend/resume, priorities and `run_at`. The code maps one-to-one onto
  `SELECT … FOR UPDATE SKIP LOCKED` in Postgres, or onto a durable-execution engine where each step is an activity.
- **Trade-off.** Single node; SQLite write throughput; workers must share a filesystem.
- **What would change my mind.** More than one machine or more than a handful of concurrent tasks: Postgres first, Temporal
  when workflows need timers, signals and versioning (see ARCHITECTURE.md, Path to production).

## D13 One tool call per step, mandatory `why`

- **Decision.** Every step is exactly one tool call; every tool's arguments include a required `why` (truncated, not rejected,
  above 200 characters). Extra tool calls in a reply are ignored with a note to the model.
- **Alternatives.** Parallel tool calls; plan-then-execute batches; optional reasoning text.
- **Why.** One action per observation keeps the agent honest about state (the page after a click is different), makes every
  step checkpointable and resumable, and makes the trace readable: each line says what was done and why. The `why` is what a
  manager reads in `karya show --trace`.
- **Trade-off.** More model calls than batched execution; slower on long batches.
- **What would change my mind.** Independent read-only lookups (e.g. five API searches) could safely run in parallel; writes
  never.

## D14 Read APIs for lookups, browser for UI-only actions (tool selection)

- **Decision.** Connectors may declare read-only API endpoints (`api_get`), used for lookups (vendor search, duplicate checks,
  verification). Actions are performed in the browser, the way an employee does them. The system prompt states this preference.
- **Alternatives.** Browser only; APIs for everything including writes.
- **Why.** Lookups through an API are faster, cheaper and unambiguous (exact JSON instead of a parsed page), and the gateway's
  reconcile/duplicate checks use the same endpoints. Writes go through the UI because that is what real companies expose to a
  "new employee" (often no write API exists) and because the form is where business validation lives.
- **Trade-off.** Two ways to read the same data; the model sometimes picks the slower one (it still works).
- **What would change my mind.** Systems with good write APIs and no UI requirement: then writes would go through declared API
  actions under the same gateway pipeline.

## D15 Sandbox company with chaos instead of real sites (brief compliance, reproducible evals)

- **Decision.** A fictional company (Meridian Components Pvt Ltd) with three FastAPI apps (ERP, AP mailbox, HR portal), a
  shared drive, seed data, generated invoice PDFs, and a chaos middleware (profiles `off/light/heavy` and armed faults
  `http_500`, `slow`, `session_expired`, `label_drift`, `commit_then_504`, `write_conflict_409`). `karya sandbox reset` restores
  a known state.
- **Alternatives.** Real SaaS demo accounts; recorded pages; mocks.
- **Why.** The brief forbids real systems and credentials, and evals need ground truth and repeatability: the same scenario,
  the same seed data, measurable outcomes read straight from the apps' databases. Chaos makes reliability testable on demand
  instead of hoping a real site misbehaves during the demo.
- **Trade-off.** The sandbox is simpler than real ERPs (no SSO, no SPA framework quirks, no rate limits); results transfer only
  as far as the sandbox resembles reality.
- **What would change my mind.** A design partner's staging environment would be the next target, behind the same connectors.

## D16 Money as integer paise; deterministic normalization after LLM extraction

- **Decision.** Amounts are stored as integer paise in the sandbox and compared as `Decimal` in the runtime. Extraction from
  documents is done by the fast model (a forced `submit_<schema>` call built from `company/schemas/*.json`, values
  copied as printed), then normalized in code (`karya/tools/parsing.py`): Indian digit grouping (`1,84,500.00`),
  `INR`/`Rs.`/`₹`, day-first dates (`03/10/2026`, `3 Oct 2026`, `2026-10-03`), followed by the schema's checks
  (subtotal + GST = total, due date after invoice date).
- **Alternatives.** Floats; trust the model's normalized output.
- **Why.** Floats lose paise; models misread `1,84,500` as 184.5 or flip day and month. Deterministic normalization after
  extraction gives the model the reading job and code the arithmetic, and lets the verifier compare exact values.
- **Trade-off.** Parsers for each format; ambiguous dates are resolved by company convention (day-first), stated in the
  assumptions.
- **What would change my mind.** Nothing for storage. For parsing, structured e-invoices (IRN JSON) would replace PDF extraction.

## D17 Forced function calling for structured outputs (portable across providers)

- **Decision.** Every structured output (Goal Spec, document extraction, learned facts, injection check) is a forced tool call
  validated by pydantic, with one retry that includes the validation error (`karya/llm/base.py:structured`); the verifier's
  verdicts are a `submit_verdicts` tool call validated the same way. Schemas are inlined (no `$ref`) so the same tool
  definitions work on OpenAI, Azure OpenAI and Anthropic.
- **Alternatives.** JSON mode / response_format; provider-specific structured output; parsing free text.
- **Why.** Forced tool calls are supported the same way by every major provider, so the runtime stays provider-agnostic, and
  pydantic validation turns "the model returned something odd" into a precise error the model can fix in one retry.
- **Trade-off.** Tool-call schemas are less expressive than full JSON Schema on some providers (hence the inlining); one retry
  costs a call.
- **What would change my mind.** If one provider were chosen permanently, its native strict structured-output mode would remove
  the retry path.

## D18 Stateless steps rebuilt from the ledger

- **Decision.** Each step sends two messages: a system message (instructions + company context, re-retrieved only when the plan
  changes or every 10 steps, so it is cacheable) and one user message rebuilt from the append-only ledger (task state, Goal
  Spec, plan, facts, decisions, answers, history with older steps compressed to one line, the latest observation in full, a
  budget line). No provider-side transcript is kept (`karya/runtime/context.py:build`).
- **Alternatives.** Append to a growing chat transcript with tool-result messages; summarise with a model.
- **Why.** Resume after a crash or a three-day wait for approval is the same code path as the next step of a live run. Context
  stays bounded (~4k tokens per step measured), untrusted content is wrapped consistently, and switching provider mid-task is
  trivial.
- **Trade-off.** The model doesn't see its own earlier raw tool outputs beyond the last few steps; important values must be
  recorded as task facts (`record_fact`), which the prompt asks for.
- **What would change my mind.** Tasks where long verbatim history matters more than structure (e.g. drafting a long document);
  then the history window would grow, still rebuilt from the ledger.

## D19 Duplicate check before approval holds; the system of record decides "committed"

- **Decision.** The gateway runs the duplicate and pre-write existence checks *before* creating an approval hold. For writes with
  a reconcile spec, a 2xx/3xx response is classified `committed` only if the lookup finds the record (which also yields the record
  reference, e.g. `ERP-INV-000104`). A redirect to the connector's logged-out path (`/login`) is `failed` ("the session had
  expired and the form was NOT submitted"), not `committed`.
- **Alternatives.** Approval first, duplicates later; trust the status code.
- **Why.** Nobody should be asked to approve a duplicate payment. Status codes lie in both directions: a 303 to `/login` looks like
  success to a naive client, and a 504 can hide a committed write. Asking the system of record is the only honest answer.
- **Trade-off.** One extra lookup per write.
- **What would change my mind.** Systems that return the created record's id in a reliable, signed response.

## D20 Reports merge code-made facts, not only the model's summary

- **Decision.** `karya/runtime/report.py:build` writes `report.md`/`report.json` from both the model's `finish` arguments and facts
  the runtime knows: detector flags (`prompt_injection`, `possible_fraud`), committed writes and record refs from the intent log,
  approvals, policy events, verifier verdicts, system evidence, learned facts that were in context ("Company knowledge used"),
  screenshots and stats.
- **Alternatives.** Let the model write the report.
- **Why.** The Apex invoice carries hidden text asking the agent to omit the note from its summary. Whatever the model writes, the
  flag is in the report because code put it there. The same goes for every number a human will act on.
- **Trade-off.** Reports are more structured than prose.
- **What would change my mind.** Nothing; prose summaries can be added on top.

## D21 Scripted agents as data for tests and red-team probes

- **Decision.** `ScriptedLLM` replays a list of tool calls (refs resolved from the latest snapshot by role/name) and
  `ScriptedVerifier` returns verdicts, so the full runtime — real Chromium, the gateway, the sandbox apps, the queue, the verifier's
  deterministic evidence, reports — is tested without a model. Scripted scenarios are YAML (`evals/suite.yaml` `redteam-bank`,
  `tests/eval_suite_scripted.yaml`).
- **Alternatives.** Mock the browser; record/replay model responses; test only with a live model.
- **Why.** Deterministic, free, fast tests for everything that must not depend on model behaviour (159 passing tests). A new
  red-team probe is a YAML list of actions, no code.
- **Trade-off.** Scripts test the runtime, not the model's judgement; that is what the eval suite with a real model measures.
- **What would change my mind.** Nothing; both layers are needed.

## D22 Fail closed on uncertainty inside the gateway

- **Decision.** Every uncertain path in the gateway resolves to "not sent": an exception in `decide()` → 500 page, request not sent;
  a policy condition that can't be evaluated (missing or malformed field) counts as matching; a failed pre-write lookup → 503;
  an earlier attempt whose outcome is still unknown blocks resubmission; an identical write already in flight is refused.
- **Alternatives.** Fail open with a warning; let the model decide.
- **Why.** The gateway is the guarantee the rest of the system leans on (D3). A guarantee that holds "unless something odd happens"
  is not one, and odd things are what chaos and attackers produce.
- **Trade-off.** Transient infrastructure problems stop writes instead of degrading gracefully; the agent is told to try again later.
- **What would change my mind.** Nothing for writes.

## D23 Evaluation design: ground truth from the databases, not from the agent

- **Decision.** `karya/evals/checks.py` reads the sandbox SQLite databases (read-only) and the runtime DB directly, with
  before-snapshots taken right after reset (bank details, vendor count). `memory: clear` also clears episodes. Scripted scenarios
  run only without chaos. Verifier agreement is computed only for model-driven runs. Evals use the main runtime DB so `karya show`
  and `karya audit` work on eval tasks.
- **Alternatives.** Use the agent's own verifier as the score; check through the apps' UI.
- **Why.** An eval that trusts the system under test measures nothing. Clearing episodes matters: a past "Acme Industries" episode
  would otherwise leak the answer into `ap-ambiguous-ask`. A fixed script cannot adapt to chaos, so running it under chaos would
  measure the script, not the system.
- **Trade-off.** Checks are coupled to the sandbox schemas; a generic `sql` check keeps new scenarios code-free.
- **What would change my mind.** Real deployments would evaluate on replayed production traces with human-labelled outcomes.
