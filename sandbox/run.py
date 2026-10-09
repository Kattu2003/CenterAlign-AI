"""Start, stop, reset and inspect the sandbox apps.

Cross-platform by construction: apps are started with `sys.executable -m
uvicorn ...` (no shell scripts), PIDs go to `sandbox/data/pids.json`.
Usable as `karya sandbox ...` or `python -m sandbox.run up|down|reset|status`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date

import httpx

from sandbox.common import settings


@dataclass(frozen=True)
class AppSpec:
    name: str
    module: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


APPS: list[AppSpec] = [
    AppSpec("erp", "sandbox.erp.app:app", 8101),
    AppSpec("mail", "sandbox.mail.app:app", 8102),
    AppSpec("hr", "sandbox.hr.app:app", 8103),
]


def get_app(name: str) -> AppSpec:
    for spec in APPS:
        if spec.name == name:
            return spec
    raise ValueError(f"unknown sandbox app {name!r}; choose from {[a.name for a in APPS]}")


def _pid_file():
    return settings.data_dir() / "pids.json"


def _read_pids() -> dict[str, int]:
    path = _pid_file()
    return json.loads(path.read_text()) if path.exists() else {}


def _write_pids(pids: dict[str, int]) -> None:
    _pid_file().write_text(json.dumps(pids, indent=2))


def is_up(spec: AppSpec, timeout: float = 1.5) -> bool:
    try:
        return httpx.get(f"{spec.base_url}/login", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def up(names: list[str] | None = None, wait_s: float = 30.0) -> list[tuple[str, str]]:
    """Start each app unless it already responds; wait until /login returns 200."""
    results: list[tuple[str, str]] = []
    pids = _read_pids()
    log_dir = settings.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    started: list[AppSpec] = []
    for spec in [get_app(n) for n in names] if names else APPS:
        if is_up(spec):
            results.append((spec.name, "already up"))
            continue
        kwargs: dict = {}
        if os.name == "nt":
            # CREATE_NO_WINDOW, not DETACHED_PROCESS: a venv's python.exe is a launcher that starts the real
            # interpreter as a child, and that child would get a console window of its own (closing it kills the app).
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True  # survive the CLI exiting; ignore its Ctrl-C
        log = open(log_dir / f"{spec.name}.log", "ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", spec.module, "--host", "127.0.0.1", "--port", str(spec.port), "--log-level", "warning"],
            cwd=str(settings.ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        pids[spec.name] = proc.pid
        started.append(spec)
    _write_pids(pids)
    deadline = time.monotonic() + wait_s
    for spec in started:
        while not is_up(spec):
            if time.monotonic() > deadline:
                results.append((spec.name, f"FAILED to start (see {log_dir / (spec.name + '.log')})"))
                break
            time.sleep(0.3)
        else:
            results.append((spec.name, f"started on {spec.base_url} (pid {pids[spec.name]})"))
    return results


def _stop(pid: int) -> bool:
    """Stop one app. On Windows, the whole process tree: the venv launcher and the interpreter it started."""
    if os.name == "nt":
        return subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True).returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, OSError):
        return False


def down() -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    pids = _read_pids()
    for name, pid in pids.items():
        results.append((name, f"stopped (pid {pid})" if _stop(pid) else f"not running (pid {pid})"))
    deadline = time.monotonic() + 8
    for name in pids:
        spec = get_app(name)
        while is_up(spec, timeout=0.5) and time.monotonic() < deadline:
            time.sleep(0.2)
    if _pid_file().exists():
        _pid_file().unlink()
    return results


def _admin(spec: AppSpec, method: str, path: str, body: dict | None = None) -> dict:
    headers = {"X-Sandbox-Admin": settings.admin_token()}
    response = httpx.request(method, f"{spec.base_url}/__admin{path}", json=body, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def reset() -> date:
    """Re-seed databases, PDFs and shared drive; clear chaos on running apps.
    Never touches the Karya runtime database or company memory."""
    from sandbox.seed.seed import reset_all

    base = reset_all()
    for spec in APPS:
        if is_up(spec):
            _admin(spec, "POST", "/chaos/disarm")
            _admin(spec, "POST", "/chaos", {"profile": "off", "seed": 0})
    return base


def status() -> list[dict]:
    pids = _read_pids()
    return [{"app": s.name, "url": s.base_url, "up": is_up(s), "pid": pids.get(s.name)} for s in APPS]


def set_chaos(profile: str, seed: int = 0, names: list[str] | None = None) -> dict[str, dict]:
    out = {}
    for spec in [get_app(n) for n in names] if names else APPS:
        if is_up(spec):
            out[spec.name] = _admin(spec, "POST", "/chaos", {"profile": profile, "seed": seed})["chaos"]
    return out


def arm(app: str, fault: str, count: int = 1, path: str | None = None) -> dict:
    return _admin(get_app(app), "POST", "/chaos/arm", {"fault": fault, "count": count, "path": path})["chaos"]


def disarm(names: list[str] | None = None) -> None:
    for spec in [get_app(n) for n in names] if names else APPS:
        if is_up(spec):
            _admin(spec, "POST", "/chaos/disarm")


def admin_state(app: str) -> dict:
    return _admin(get_app(app), "GET", "/state")


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    if command == "up":
        for name, msg in up():
            print(f"{name}: {msg}")
    elif command == "down":
        for name, msg in down():
            print(f"{name}: {msg}")
    elif command == "reset":
        print(f"reset; D = {reset().isoformat()}")
    elif command == "status":
        for row in status():
            print(f"{row['app']}: {'up' if row['up'] else 'down'} {row['url']}")
    else:
        print("usage: python -m sandbox.run up|down|reset|status")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
