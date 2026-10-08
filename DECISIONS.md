# DECISIONS.md

Each entry: **Decision · Alternatives considered · Why · Trade-off · What would change my mind.**

## D1 Hand-rolled loop instead of LangGraph/CrewAI/browser-use
## D2 Text snapshots with element refs instead of pixel/vision control; screenshots as evidence
## D3 Enforcement in a network-level gateway rather than in prompts
## D4 Fail-closed allowlist of hosts and declared write actions
## D5 Approvals bound to a payload scope hash and single-use
## D6 Write-intent log + reconciliation for unknown outcomes; never blind write retries
## D7 Pre-write existence check as a deterministic duplicate backstop
## D8 Goal Spec with success criteria before acting
## D9 Independent read-only verifier + deterministic lookups
## D10 Company-specific config separated from the runtime (`company/`)
## D11 Memory: authored handbook vs learned facts with provenance; humans can forget facts
## D12 SQLite queue with leases (and how it maps to production)
## D13 One tool call per step, mandatory `why`
## D14 Read APIs for lookups, browser for UI-only actions (tool selection)
## D15 Sandbox company with chaos instead of real sites (brief compliance, reproducible evals)
## D16 Money as integer paise; deterministic normalization after LLM extraction
## D17 Forced function calling for structured outputs (portable across providers)
