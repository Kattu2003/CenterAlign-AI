"""Pydantic schema for connector specs (`company/connectors/*.yaml`).

A connector is metadata + a credentials reference + risk declarations, not code
(see DECISIONS.md D10). The browser tool drives any web app generically; the
gateway uses the spec to classify and police every request.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator

RiskLevel = Literal["auth", "read", "write_low", "write", "irreversible"]
FieldType = Literal["str", "decimal", "date", "int"]

_TEMPLATE_PARAM = re.compile(r"(\{\w+\})")


def compile_template(template: str) -> re.Pattern[str]:
    """'/vendors/{vendor_id}/bank' -> ^/vendors/(?P<vendor_id>[^/]+)/bank$"""
    parts = []
    for token in _TEMPLATE_PARAM.split(template):
        if _TEMPLATE_PARAM.fullmatch(token):
            parts.append(f"(?P<{token[1:-1]}>[^/]+)")
        else:
            parts.append(re.escape(token))
    return re.compile("^" + "".join(parts) + "$")


def template_params(template: str) -> list[str]:
    return [token[1:-1] for token in _TEMPLATE_PARAM.findall(template)]


def normalize_origin(url: str) -> str:
    """'HTTP://127.0.0.1:8101/x' -> 'http://127.0.0.1:8101' (default ports made explicit)."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    return f"{scheme}://{host}:{port}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Strict):
    type: FieldType = "str"
    description: str = ""


class ReconcileSpec(_Strict):
    """How to find out whether a write really happened (DECISIONS.md D6)."""

    api: str                                   # a declared API endpoint of the same connector
    params: dict[str, str] = {}                # values are templates over the write's fields
    exists_if: Literal["non_empty_list"] | None = None
    match: dict[str, str] | None = None        # exists only if some item matches every field
    record_ref_field: str | None = None        # e.g. erp_ref

    @model_validator(mode="after")
    def _one_test(self) -> "ReconcileSpec":
        if (self.exists_if is None) == (self.match is None):
            raise ValueError("reconcile needs exactly one of exists_if or match")
        return self


class ActionSpec(_Strict):
    name: str
    method: str
    path: str
    risk: RiskLevel
    description: str = ""
    fields: dict[str, FieldSpec] = {}
    idempotency_key: list[str] = []
    reconcile: ReconcileSpec | None = None
    display: str | None = None

    _pattern: re.Pattern[str] = PrivateAttr()

    def model_post_init(self, __context: object) -> None:
        self.method = self.method.upper()
        self._pattern = compile_template(self.path)

    def match_path(self, path: str) -> dict[str, str] | None:
        match = self._pattern.match(path)
        return match.groupdict() if match else None


class Credentials(_Strict):
    username_env: str
    password_env: str


class LoggedOutSignal(_Strict):
    path_prefix: str


class AuthSpec(_Strict):
    type: Literal["form_login"] = "form_login"
    login_path: str = "/login"
    form_fields: dict[str, str] = {"username": "username", "password": "password"}
    credentials: Credentials
    logged_out_signal: LoggedOutSignal | None = None


class ApiAuth(_Strict):
    header: str
    key_env: str


class ApiEndpoint(_Strict):
    name: str
    path: str
    params: list[str] = []
    description: str = ""

    @property
    def path_params(self) -> list[str]:
        return template_params(self.path)


class ApiSpec(_Strict):
    auth: ApiAuth
    endpoints: list[ApiEndpoint] = []

    def endpoint(self, name: str) -> ApiEndpoint | None:
        return next((e for e in self.endpoints if e.name == name), None)


class ConnectorSpec(_Strict):
    id: str
    name: str
    kind: Literal["web_app", "filesystem"]
    purpose: str = ""
    base_url: str | None = None
    root: str | None = None
    auth: AuthSpec | None = None
    volatile_fields: list[str] = []
    blocked_paths: list[str] = []
    actions: list[ActionSpec] = []
    api: ApiSpec | None = None
    read: list[str] = []
    write: list[str] = []

    @model_validator(mode="after")
    def _kind_fields(self) -> "ConnectorSpec":
        if self.kind == "web_app" and not self.base_url:
            raise ValueError(f"web_app connector {self.id!r} needs base_url")
        if self.kind == "filesystem" and not self.root:
            raise ValueError(f"filesystem connector {self.id!r} needs root")
        return self

    @property
    def origin(self) -> str | None:
        return normalize_origin(self.base_url) if self.base_url else None

    def action(self, name: str) -> ActionSpec | None:
        return next((a for a in self.actions if a.name == name), None)

    def match_action(self, method: str, path: str) -> tuple[ActionSpec, dict[str, str]] | None:
        """Find the declared action for a request (method + path template)."""
        for action in self.actions:
            if action.method == method.upper():
                params = action.match_path(path)
                if params is not None:
                    return action, params
        return None

    def is_blocked_path(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.blocked_paths)
