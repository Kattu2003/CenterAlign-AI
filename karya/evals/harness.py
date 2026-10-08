"""Evaluation harness (plan §14): scenarios × chaos profiles × runs.

For each run: reset the sandbox, set memory (clear/keep), set a chaos profile
with a run-specific seed, enqueue the request and run it in-process with an
auto-responder (answers questions by text match, decides approvals with a
person who holds the right role), then score it with ground-truth checks that
read the sandbox databases directly — independent of the agent's verifier.
Also measures verifier agreement: did the verifier's verdict match the truth?
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from karya.company import Company
from karya.config import Settings
from karya.evals import checks
from karya.evals.scripted import build_llm as build_scripted_llm
from karya.gateway import approvals
from karya.llm.base import LLMClient
from karya.llm.scripted import ScriptedVerifier
from karya.memory import store
from karya.memory.learning import learner_for
from karya.runtime import ledger
from karya.runtime.loop import RunContext
from karya.worker import inbox, queue
from karya.worker.worker import process_task

DEFAULT_TIMEOUT_S = 900
FALLBACK_ANSWER = "No preference from me — please follow the handbook and use your best judgement."


@dataclass
class Scenario:
    id: str
    request: str
    setup: dict[str, Any] = field(default_factory=lambda: {"reset": True, "memory": "clear"})
    human: dict[str, Any] = field(default_factory=lambda: {"answers": [], "approvals": "none"})
    expect: dict[str, Any] = field(default_factory=dict)
    scripted: dict[str, Any] | None = None
    requester: str | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S


def load_suite(path: Path) -> list[Scenario]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [Scenario(**item) for item in raw]


class SandboxEnvironment:
    """The sandbox company, controlled through `sandbox.run` (the apps must be up)."""

    def reset(self) -> date:
        from sandbox import run

        down = [s["app"] for s in run.status() if not s["up"]]
        if down:
            raise RuntimeError(f"sandbox apps not running: {down}. Start them with `karya sandbox up`.")
        return run.reset()

    def seed_date(self) -> date:
        from sandbox.seed.seed import read_meta

        return date.fromisoformat(read_meta()["D"])

    def set_chaos(self, profile: str, seed: int) -> None:
        from sandbox import run

        run.set_chaos(profile, seed)

    def db_path(self, app: str) -> Path:
        from sandbox.common import settings

        return settings.db_path(app)


@dataclass
class RunRecord:
    scenario: str
    chaos: str
    run: int
    task_id: str | None = None
    status: str = ""
    expected_status: list[str] = field(default_factory=list)
    passed: bool = False
    skipped: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    verifier_overall: str | None = None
    verifier_agrees: bool | None = None
    steps: int = 0
    duration_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_errors: int = 0
    get_retries: int = 0
    relogins: int = 0
    reconciliations: int = 0
    policy_blocks: int = 0
    questions_asked: int = 0
    error: str | None = None
    note: str = ""


class Harness:
    def __init__(
        self,
        settings: Settings,
        company: Company,
        conn: sqlite3.Connection,
        llm: LLMClient | None,
        env: Any,
        out_dir: Path,
        progress: Callable[[str], None] = print,
    ) -> None:
        self.settings = settings
        self.company = company
        self.conn = conn
        self.llm = llm
        self.env = env
        self.out_dir = out_dir
        self.progress = progress

    # --- one run ---------------------------------------------------------------------------------

    def run_one(self, sc: Scenario, profile: str, run_index: int) -> RunRecord:
        record = RunRecord(scenario=sc.id, chaos=profile, run=run_index, expected_status=list(sc.expect.get("status", [])))
        if sc.scripted is None and self.llm is None:
            record.skipped = "no LLM configured"
            return record
        if sc.scripted is not None and profile != "off":
            record.skipped = "scripted scenarios run without chaos"
            return record
        base = self.env.reset() if sc.setup.get("reset", True) else self.env.seed_date()
        if sc.setup.get("memory", "clear") == "clear":
            store.deactivate_all(self.conn, by="evals")
            store.clear_episodes(self.conn)  # a past episode would leak answers into "ask" scenarios
        specs = sc.expect.get("checks", [])
        pre = checks.CheckContext(self.env.db_path, self.conn, "", base, {})
        before = checks.snapshot_before(pre, specs)
        self.env.set_chaos(profile, zlib.crc32(f"{sc.id}:{run_index}".encode()) % 100_000)
        if sc.scripted is not None:
            llm, verifier = build_scripted_llm(sc.scripted, self.company, base), ScriptedVerifier()
        else:
            llm, verifier = self.llm, None
        ctx = RunContext(settings=self.settings, company=self.company, llm=llm, conn=self.conn, worker_id="eval", verifier_llm=verifier)
        requester = sc.requester or self.company.profile.default_requester
        task_id = queue.enqueue(self.conn, sc.request, requester)
        record.task_id = task_id
        started = time.monotonic()
        deadline = started + sc.timeout_s
        try:
            while True:
                if time.monotonic() > deadline:
                    record.error = f"timeout after {sc.timeout_s}s"
                    break
                if queue.claim(self.conn, ctx.worker_id, task_id=task_id) is not None:
                    process_task(task_id, ctx)
                    continue
                status = queue.get(self.conn, task_id)["status"]
                if status in queue.TERMINAL:
                    break
                if status == "waiting_human":
                    self._answer(sc, task_id, requester)
                elif status == "waiting_approval":
                    self._decide(sc, task_id, record)
                else:
                    time.sleep(0.5)
        finally:
            self.env.set_chaos("off", 0)
        record.duration_s = round(time.monotonic() - started, 1)
        record.status = queue.get(self.conn, task_id)["status"]
        report_path = self.settings.runs_dir / task_id / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        results = checks.run_checks(checks.CheckContext(self.env.db_path, self.conn, task_id, base, report), specs, before)
        record.checks = [asdict(r) for r in results]
        checks_ok = all(r.ok for r in results)
        status_ok = not record.expected_status or record.status in record.expected_status
        record.passed = checks_ok and status_ok and record.error is None
        state = ledger.load_state(self.conn, task_id)
        if state.verifications:
            record.verifier_overall = state.verifications[-1]["result"]["overall"]
            if sc.scripted is None:
                record.verifier_agrees = (record.verifier_overall == "pass") == checks_ok
        stats = report.get("stats", {})
        task = queue.get(self.conn, task_id)
        record.steps = stats.get("steps", task["step_count"])
        record.tokens_in, record.tokens_out = task["tokens_in"], task["tokens_out"]
        for key in ("tool_errors", "get_retries", "relogins", "reconciliations", "policy_blocks", "questions_asked"):
            setattr(record, key, int(stats.get(key, 0)))
        if not record.passed:
            failed = next((r for r in results if not r.ok), None)
            record.note = record.error or (f"status {record.status}, expected {record.expected_status}" if not status_ok else f"{failed.type}: {failed.detail}" if failed else "")
        return record

    def _answer(self, sc: Scenario, task_id: str, requester: str) -> None:
        learn = learner_for(self.llm) if self.llm is not None else None
        for question in self.conn.execute("SELECT * FROM questions WHERE task_id = ? AND answer IS NULL", (task_id,)).fetchall():
            text = (question["question"] + " " + (question["options_json"] or "")).lower()
            reply = next((a for a in sc.human.get("answers", []) if str(a["match"]).lower() in text), None)
            if reply:
                inbox.answer_question(self.conn, self.company, question["id"], reply["answer"], reply.get("by", requester), reply.get("remember", True), learn=learn)
            else:
                inbox.answer_question(self.conn, self.company, question["id"], FALLBACK_ANSWER, requester, remember=False)

    def _decide(self, sc: Scenario, task_id: str, record: RunRecord) -> None:
        mode = sc.human.get("approvals", "none")
        for apr in [a for a in approvals.for_task(self.conn, task_id) if a["status"] == "pending"]:
            role = apr["approver_role"].split(",")[0]
            holders = self.company.people_with_role(role)
            approver = holders[0].name if holders else "unknown"
            if mode == "approve":
                inbox.decide(self.conn, self.company, apr["id"], True, approver, sc.human.get("approval_note", ""))
            else:
                reason = sc.human.get("reject_reason", "Rejected by the evaluation script.") if mode == "reject" else "No approval was expected in this scenario."
                if mode != "reject":
                    record.note = "unexpected approval request"
                learn = learner_for(self.llm) if self.llm is not None else None
                inbox.decide(self.conn, self.company, apr["id"], False, approver, reason, learn=learn)

    # --- the suite ---------------------------------------------------------------------------------

    def run(self, scenarios: list[Scenario], profiles: list[str], runs: int) -> dict[str, Any]:
        records: list[RunRecord] = []
        total = len(scenarios) * len(profiles) * runs
        count = 0
        for profile in profiles:
            for sc in scenarios:
                for run_index in range(1, runs + 1):
                    count += 1
                    record = self.run_one(sc, profile, run_index)
                    records.append(record)
                    if record.skipped:
                        self.progress(f"[{count}/{total}] {profile:<5} {sc.id:<22} SKIPPED ({record.skipped})")
                    else:
                        verdict = "PASS" if record.passed else "FAIL"
                        self.progress(
                            f"[{count}/{total}] {profile:<5} {sc.id:<22} {verdict} {record.status} · {record.steps} steps · "
                            f"{record.duration_s:.0f}s · {(record.tokens_in + record.tokens_out) // 1000}k tokens · {record.task_id}"
                            + (f" · {record.note}" if record.note else "")
                        )
        return self.write(records)

    def write(self, records: list[RunRecord]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        model = "scripted only" if self.llm is None else f"{self.settings.llm_provider} / {self.settings.llm_model} (fast: {self.settings.fast_model})"
        data = {"timestamp": now.isoformat(timespec="seconds"), "model": model, "summary": summarize(records), "scenarios": per_scenario(records), "runs": [asdict(r) for r in records]}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%d-%H%M%S")
        markdown = render_markdown(data)
        (self.out_dir / f"{stamp}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        (self.out_dir / f"{stamp}.md").write_text(markdown, encoding="utf-8")
        (self.out_dir / "latest.md").write_text(markdown, encoding="utf-8")
        data["files"] = [str(self.out_dir / f"{stamp}.json"), str(self.out_dir / f"{stamp}.md"), str(self.out_dir / "latest.md")]
        return data


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def summarize(records: list[RunRecord]) -> list[dict[str, Any]]:
    out = []
    for profile in dict.fromkeys(r.chaos for r in records):
        ran = [r for r in records if r.chaos == profile and not r.skipped]
        agreeing = [r for r in ran if r.verifier_agrees is not None]
        out.append({
            "chaos": profile,
            "runs": len(ran),
            "passed": sum(r.passed for r in ran),
            "pass_rate": round(sum(r.passed for r in ran) / len(ran), 3) if ran else None,
            "verifier_agreement": round(sum(r.verifier_agrees for r in agreeing) / len(agreeing), 3) if agreeing else None,
            "median_steps": _median([r.steps for r in ran]),
            "median_tokens": _median([r.tokens_in + r.tokens_out for r in ran]),
            "skipped": sum(1 for r in records if r.chaos == profile and r.skipped),
        })
    return out


def per_scenario(records: list[RunRecord]) -> list[dict[str, Any]]:
    out = []
    for key in dict.fromkeys((r.scenario, r.chaos) for r in records):
        group = [r for r in records if (r.scenario, r.chaos) == key]
        ran = [r for r in group if not r.skipped]
        out.append({
            "scenario": key[0],
            "chaos": key[1],
            "runs": len(ran),
            "passes": sum(r.passed for r in ran),
            "median_steps": _median([r.steps for r in ran]),
            "median_duration_s": _median([r.duration_s for r in ran]),
            "median_tokens": _median([r.tokens_in + r.tokens_out for r in ran]),
            "retries": sum(r.get_retries for r in ran),
            "relogins": sum(r.relogins for r in ran),
            "reconciliations": sum(r.reconciliations for r in ran),
            "notes": "; ".join(sorted({r.note for r in ran if r.note})) or ("skipped: " + group[0].skipped if group and group[0].skipped else ""),
        })
    return out


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def render_markdown(data: dict[str, Any]) -> str:
    lines = [f"# Karya evaluation results — {data['timestamp']}", "", f"Model: {data['model']}", "", "## Summary", "",
             "| Chaos | Runs | Passed | Pass rate | Verifier agreement | Median steps | Median tokens | Skipped |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in data["summary"]:
        lines.append(f"| {s['chaos']} | {s['runs']} | {s['passed']} | {_pct(s['pass_rate'])} | {_pct(s['verifier_agreement'])} | {s['median_steps']:.0f} | {s['median_tokens'] / 1000:.0f}k | {s['skipped']} |")
    lines += ["", "## Per scenario", "", "| Scenario | Chaos | Runs | Passes | Median steps | Median duration | Median tokens | GET retries | Re-logins | Reconciliations | Notes |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in data["scenarios"]:
        lines.append(f"| {s['scenario']} | {s['chaos']} | {s['runs']} | {s['passes']} | {s['median_steps']:.0f} | {s['median_duration_s']:.0f}s | {s['median_tokens'] / 1000:.0f}k | {s['retries']} | {s['relogins']} | {s['reconciliations']} | {s['notes']} |")
    lines += ["", "Ground-truth checks read the sandbox databases directly; they are independent of the agent's own verifier.",
              "Verifier agreement = share of model-driven runs where the verifier's overall verdict matched the ground truth."]
    return "\n".join(lines) + "\n"
