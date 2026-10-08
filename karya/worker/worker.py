"""Runs claimed tasks: heartbeat while running, then apply the loop's result.

`karya worker` loops over `claim -> process_task`; `karya run` uses the same
`process_task` in the foreground, so both paths behave identically.
"""

from __future__ import annotations

import threading
from typing import Callable
import traceback

from karya import db
from karya.llm.base import LLMConfigError
from karya.runtime import ledger
from karya.runtime.loop import LoopResult, RunContext, run_task
from karya.worker import queue

HEARTBEAT_SECONDS = 20


def process_task(task_id: str, ctx: RunContext) -> LoopResult:
    """Run one task this worker has claimed. Never leaves the task without a status."""
    stop_beating = threading.Event()
    ctx.abort.clear()

    def beat() -> None:
        conn = db.connect(ctx.settings.db_path)  # sqlite connections are per thread
        try:
            while not stop_beating.wait(HEARTBEAT_SECONDS):
                if not queue.heartbeat(conn, task_id, ctx.worker_id):
                    ctx.abort.set()  # we lost the lease: stop this task after the current step
                    return
        finally:
            conn.close()

    heart = threading.Thread(target=beat, name=f"heartbeat-{task_id}", daemon=True)
    heart.start()
    try:
        result = run_task(task_id, ctx)
    except LLMConfigError:
        queue.release(ctx.conn, task_id, ctx.worker_id, "queued", error="LLM is not configured")
        raise
    except Exception as exc:  # log, release, retry up to MAX_ATTEMPTS
        error = f"{type(exc).__name__}: {exc}"
        ledger.append(ctx.conn, task_id, "system", {"type": "error", "text": f"Run failed: {error}", "trace": traceback.format_exc()[-4000:]})
        status = queue.fail_or_retry(ctx.conn, task_id, ctx.worker_id, error)
        return LoopResult("done" if status == "failed" else "interrupted", status, error)
    finally:
        stop_beating.set()
        heart.join(timeout=2)
    queue.release(ctx.conn, task_id, ctx.worker_id, result.status, result=result.result)
    return result


def run_worker(ctx: RunContext, once: bool = False, poll_seconds: float = 1.0, on_idle: "Callable[[], None] | None" = None) -> int:
    """Claim -> run -> repeat. One task at a time; several workers are safe (leases).

    First Ctrl-C: finish the current step, checkpoint, release the lease (the task
    goes back to `queued`). Second Ctrl-C: exit immediately. A `kill -9` is
    recovered by lease expiry: another worker reclaims the task and resumes it.
    """
    import signal
    import time

    previous = None

    def on_sigint(signum: int, frame: object) -> None:
        if ctx.stop.is_set():
            raise KeyboardInterrupt
        ctx.stop.set()

    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, on_sigint)
    processed = 0
    try:
        while not ctx.stop.is_set():
            task = queue.claim(ctx.conn, ctx.worker_id)
            if task is None:
                if once:
                    break
                if on_idle:
                    on_idle()
                time.sleep(poll_seconds)
                continue
            process_task(task["id"], ctx)
            processed += 1
            if once:
                break
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    return processed
