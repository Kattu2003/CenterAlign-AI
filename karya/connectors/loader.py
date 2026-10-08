"""Load connector specs from `company/connectors/*.yaml`."""

from __future__ import annotations

from pathlib import Path

import yaml

from karya.connectors.models import ConnectorSpec


def load_connectors(company_dir: Path) -> dict[str, ConnectorSpec]:
    connectors: dict[str, ConnectorSpec] = {}
    for path in sorted((company_dir / "connectors").glob("*.yaml")):
        spec = ConnectorSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if spec.id in connectors:
            raise ValueError(f"duplicate connector id {spec.id!r} in {path.name}")
        connectors[spec.id] = spec
    return connectors


def connector_for_origin(connectors: dict[str, ConnectorSpec], origin: str) -> ConnectorSpec | None:
    """The web-app connector whose base_url has this origin (host allowlist lookup)."""
    return next((c for c in connectors.values() if c.origin == origin), None)
