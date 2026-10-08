"""Chaos: fault injection shared by all sandbox apps (plan §7.7).

Two mechanisms:
1. Profiles (`off` / `light` / `heavy`): random faults from a seeded RNG, so a
   chaos run is reproducible given the same request sequence.
2. Armed faults: "the next N matching requests get fault X" — deterministic,
   for demos and tests.

Faults are injected by a pure ASGI middleware that sits *inside*
SessionMiddleware, so it can clear the session (session_expired) before the
app sees the request. `label_drift` only sets a seed in the request scope; the
templates read it through `Drift` and vary button text, element ids and the
order of optional fields (never `name` attributes).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import random
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

FAULTS = ("http_500", "slow", "session_expired", "label_drift", "commit_then_504", "write_conflict_409")

PROFILES: dict[str, dict[str, float]] = {
    "off": {},
    "light": {"http_500": 0.05, "slow": 0.10, "label_drift": 0.30},
    "heavy": {
        "http_500": 0.12,
        "slow": 0.15,
        "session_expired": 0.08,
        "label_drift": 1.0,
        "commit_then_504": 0.25,
        "write_conflict_409": 0.10,
    },
}

# Paths that are never subject to chaos.
_EXEMPT_PREFIXES = ("/__admin", "/static", "/favicon.ico")


@dataclass
class ArmedFault:
    fault: str
    remaining: int
    path: str | None = None  # glob; None = any applicable request

    def matches(self, path: str) -> bool:
        return self.path is None or fnmatch.fnmatchcase(path, self.path)


@dataclass
class ChaosState:
    """Per-app chaos configuration. `write_paths` / `form_paths` are globs."""

    app_name: str
    write_paths: list[str]
    form_paths: list[str]
    profile: str = "off"
    seed: int = 0
    armed: list[ArmedFault] = field(default_factory=list)
    rng: random.Random = field(default_factory=lambda: random.Random(0))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set_profile(self, profile: str, seed: int = 0) -> None:
        if profile not in PROFILES:
            raise ValueError(f"unknown chaos profile {profile!r}; choose from {sorted(PROFILES)}")
        with self.lock:
            self.profile = profile
            self.seed = seed
            self.rng = random.Random(seed)

    def arm(self, fault: str, count: int = 1, path: str | None = None) -> None:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}; choose from {list(FAULTS)}")
        with self.lock:
            self.armed.append(ArmedFault(fault=fault, remaining=max(1, count), path=path or None))

    def disarm(self) -> None:
        with self.lock:
            self.armed.clear()

    def reset(self) -> None:
        self.disarm()
        self.set_profile("off", 0)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "app": self.app_name,
                "profile": self.profile,
                "seed": self.seed,
                "armed": [{"fault": a.fault, "remaining": a.remaining, "path": a.path} for a in self.armed],
            }

    def fires(self, fault: str, path: str, use_profile: bool = True) -> bool:
        """Consume a matching armed fault, else roll the profile probability."""
        with self.lock:
            for armed in self.armed:
                if armed.fault == fault and armed.remaining > 0 and armed.matches(path):
                    armed.remaining -= 1
                    if armed.remaining == 0:
                        self.armed.remove(armed)
                    return True
            if not use_profile:
                return False
            probability = PROFILES[self.profile].get(fault, 0.0)
            return probability > 0 and self.rng.random() < probability

    def uniform(self, low: float, high: float) -> float:
        with self.lock:
            return self.rng.uniform(low, high)

    def drift_seed(self) -> int:
        with self.lock:
            return self.rng.randint(1, 10**9)

    def is_write_path(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.write_paths)

    def is_form_path(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.form_paths)


def _error_page(status: int, title: str, message: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{status} {title}</title><link rel='stylesheet' href='/static/style.css'></head>"
        f"<body><main class='container'><h1>{status} {title}</h1>"
        f"<p class='banner error' role='alert'>{message}</p></main></body></html>"
    ).encode()


class ChaosMiddleware:
    """Pure ASGI middleware. `audit` is called as audit(fault, method, path) for every injection."""

    def __init__(self, app: Any, state: ChaosState, audit: Callable[[str, str, str], None]) -> None:
        self.app = app
        self.state = state
        self.audit = audit

    async def _respond(self, send: Any, status: int, body: bytes, content_type: str) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        state = self.state
        path, method = scope["path"], scope["method"]
        is_get = method in ("GET", "HEAD")
        is_api = path.startswith("/api/")
        is_write = method == "POST" and state.is_write_path(path)
        is_form_get = is_get and state.is_form_path(path)

        if state.fires("slow", path):
            delay = state.uniform(3.0, 8.0)
            self.audit("slow", method, path)
            await asyncio.sleep(delay)

        if is_get and state.fires("http_500", path):
            self.audit("http_500", method, path)
            if is_api:
                await self._respond(send, 500, json.dumps({"error": "internal server error"}).encode(), "application/json")
            else:
                body = _error_page(500, "Internal Server Error", "Something went wrong. Please try again.")
                await self._respond(send, 500, body, "text/html; charset=utf-8")
            return

        # Profiles expire sessions on form pages only; armed faults may also hit POSTs.
        if path not in ("/login", "/logout") and (is_form_get or method == "POST"):
            if state.fires("session_expired", path, use_profile=is_form_get):
                self.audit("session_expired", method, path)
                session = scope.get("session")
                if session is not None:
                    session.clear()

        if is_write and state.fires("write_conflict_409", path):
            self.audit("write_conflict_409", method, path)
            body = _error_page(409, "Conflict", "Record locked, try again.")
            await self._respond(send, 409, body, "text/html; charset=utf-8")
            return

        if is_get and not is_api and state.fires("label_drift", path):
            self.audit("label_drift", method, path)
            scope.setdefault("state", {})["drift"] = state.drift_seed()

        if not is_write:
            await self.app(scope, receive, send)
            return

        # commit_then_504: let the app commit, then replace a success response with a 504.
        swapped = False

        async def send_wrapper(message: dict) -> None:
            nonlocal swapped
            if message["type"] == "http.response.start":
                if message["status"] < 400 and state.fires("commit_then_504", path):
                    swapped = True
                    self.audit("commit_then_504", method, path)
                    body = _error_page(504, "Gateway Timeout", "The server did not respond in time.")
                    await self._respond(send, 504, body, "text/html; charset=utf-8")
                    return
            elif message["type"] == "http.response.body" and swapped:
                return  # swallow the original body
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --- label drift (read by templates) ---------------------------------------------------------

BUTTON_VARIANTS: dict[str, list[str]] = {
    "Post invoice": ["Post invoice", "Save", "Submit entry", "Post"],
    "Apply label": ["Apply label", "Add label", "Save", "Submit"],
    "Sign in": ["Sign in", "Log in", "Login", "Continue"],
    "Update bank details": ["Update bank details", "Save changes", "Submit change"],
    "Create vendor": ["Create vendor", "Save", "Submit"],
    "Create employee": ["Create employee", "Save", "Submit"],
    "Request access": ["Request access", "Submit request", "Save"],
    "Request equipment": ["Request equipment", "Submit request", "Save"],
    "Search": ["Search", "Find", "Go", "Filter"],
}


class Drift:
    """Deterministic per-page variation. Inactive (identity) unless label_drift fired."""

    def __init__(self, seed: int | None) -> None:
        self.active = bool(seed)
        self._rng = random.Random(seed)
        self._suffix = f"{(seed or 0) % 99991:05d}"

    def button(self, label: str) -> str:
        if not self.active:
            return label
        return self._rng.choice(BUTTON_VARIANTS.get(label, [label, "Save", "Submit"]))

    def id(self, name: str) -> str:
        return f"f{self._suffix}-{name}" if self.active else name

    def order(self, items: list[str]) -> list[str]:
        if not self.active:
            return list(items)
        shuffled = list(items)
        self._rng.shuffle(shuffled)
        return shuffled


def drift_for(request: Any) -> Drift:
    """Template helper: {% set d = drift(request) %}. Cached per request so ids stay consistent."""
    scope_state = request.scope.setdefault("state", {})
    cached = scope_state.get("_drift_obj")
    if cached is None:
        cached = Drift(scope_state.get("drift"))
        scope_state["_drift_obj"] = cached
    return cached
