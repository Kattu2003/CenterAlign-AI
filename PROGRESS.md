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
