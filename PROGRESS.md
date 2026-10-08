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
- Branch: the plan says commit on `main`; this build commits to the session branch
  `claude/pensive-wright-agon4r` (one commit per phase, with the plan's commit messages), to be merged.

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
