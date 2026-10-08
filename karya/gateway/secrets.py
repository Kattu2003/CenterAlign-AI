"""Credential resolution and redaction.

Connector specs name environment variables, never values. Credentials are
resolved only inside tools (browser_login) and the gateway (API key header),
and never returned to the model: every observation passes through `redact`
before it is logged or shown (DECISIONS.md D3).
"""

from __future__ import annotations

import os
from typing import Mapping

from karya.config import env_str
from karya.connectors.models import ConnectorSpec
from karya.runtime.models import Observation

ALWAYS_SECRET = ("SANDBOX_ADMIN_TOKEN", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


class MissingSecret(RuntimeError):
    pass


class Secrets:
    def __init__(self, connectors: Mapping[str, ConnectorSpec], env: Mapping[str, str] | None = None) -> None:
        self._env = env if env is not None else os.environ
        names = set(ALWAYS_SECRET)
        for spec in connectors.values():
            if spec.auth:
                names.add(spec.auth.credentials.password_env)  # usernames are not secret
            if spec.api:
                names.add(spec.api.auth.key_env)
        self._secret_names = names

    def _get(self, name: str) -> str:
        value = self._env.get(name, "").strip()
        if not value or value.startswith("#"):
            raise MissingSecret(f"{name} is not set (copy .env.example to .env)")
        return value

    def login_credentials(self, connector: ConnectorSpec) -> tuple[str, str]:
        if not connector.auth:
            raise MissingSecret(f"connector {connector.id} has no login")
        creds = connector.auth.credentials
        return self._get(creds.username_env), self._get(creds.password_env)

    def api_header(self, connector: ConnectorSpec) -> dict[str, str]:
        if not connector.api:
            raise MissingSecret(f"connector {connector.id} has no API")
        return {connector.api.auth.header: self._get(connector.api.auth.key_env)}

    def _values(self) -> list[str]:
        values = []
        for name in self._secret_names:
            value = env_str(name) if self._env is os.environ else self._env.get(name, "")
            if value and len(value) >= 4:
                values.append(value)
        return sorted(values, key=len, reverse=True)  # longest first, so substrings don't break redaction

    def redact(self, text: str) -> str:
        for value in self._values():
            if value in text:
                text = text.replace(value, "[REDACTED]")
        return text

    def redact_observation(self, obs: Observation) -> Observation:
        return obs.model_copy(
            update={
                "summary": self.redact(obs.summary),
                "content": self.redact(obs.content),
                "error": self.redact(obs.error) if obs.error else obs.error,
            }
        )
