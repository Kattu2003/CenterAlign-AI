"""The worker's Ctrl-C handling in a real process: the first SIGINT stops it gracefully."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

SCRIPT = r"""
import sys
from karya import db
from karya.company import load_company
from karya.config import get_settings
from karya.llm.scripted import ScriptedLLM
from karya.runtime.loop import RunContext
from karya.worker.worker import run_worker
ctx = RunContext(settings=get_settings(), company=load_company(), llm=ScriptedLLM(), conn=db.connect(), worker_id="sig")
print("ready", flush=True)
n = run_worker(ctx, poll_seconds=0.1)
print("stopped" if ctx.stop.is_set() else "exited", n, flush=True)
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_first_sigint_stops_worker_gracefully(tmp_path):
    env = {**os.environ, "KARYA_DB_PATH": str(tmp_path / "sig.db")}
    proc = subprocess.Popen([sys.executable, "-c", SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(0.3)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert out.strip() == "stopped 0"
