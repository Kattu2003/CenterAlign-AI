"""Load everything company-specific from one directory (DECISIONS.md D10).

`company/` holds the profile and people, the handbook, the policies, the
connectors and the document schemas. A different company is a different
directory; the runtime code does not change.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from karya.config import get_settings, resolve_path
from karya.connectors.loader import connector_for_origin, load_connectors
from karya.connectors.models import ConnectorSpec, template_params
from karya.gateway.policy import PolicySet, load_policies


class Person(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    title: str = ""
    roles: list[str] = []


class CompanyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    short_name: str = ""
    timezone: str = "UTC"
    currency: str = ""
    email_domain: str = ""
    people: list[Person] = []
    default_requester: str


class SchemaField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["string", "date", "decimal", "int"] = "string"
    description: str = ""
    required: bool = True


class DocumentSchema(BaseModel):
    """A document type the agent can extract (`files_extract`). Schemas are data."""

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    fields: dict[str, SchemaField]
    checks: list[dict[str, Any]] = []


@dataclass
class HandbookChunk:
    id: str          # 'ap-01#5' (chunk 0 is the title + purpose)
    doc_id: str
    doc_title: str
    heading: str
    text: str


@dataclass
class HandbookDoc:
    id: str
    title: str
    path: Path
    chunks: list[HandbookChunk] = field(default_factory=list)


def _doc_id(stem: str) -> str:
    """'ap-01-invoice-processing' -> 'ap-01'; '00-company-overview' -> 'company-overview'."""
    match = re.match(r"^([a-z]+-\d+)", stem)
    if match:
        return match.group(1)
    return re.sub(r"^\d+-", "", stem)


def parse_handbook(path: Path) -> HandbookDoc:
    """Chunk a handbook file on `##` headings (the retriever indexes chunks)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    doc_id = _doc_id(path.stem)
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), path.stem)
    doc = HandbookDoc(id=doc_id, title=title, path=path)
    heading, buffer = title, []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            doc.chunks.append(HandbookChunk(f"{doc_id}#{len(doc.chunks)}", doc_id, title, heading, text))

    for line in lines:
        if line.startswith("## "):
            flush()
            heading, buffer = line[3:].strip(), [line]
        elif not line.startswith("# "):
            buffer.append(line)
    flush()
    return doc


@dataclass
class Company:
    dir: Path
    profile: CompanyProfile
    handbook: list[HandbookDoc]
    policies: PolicySet
    connectors: dict[str, ConnectorSpec]
    schemas: dict[str, DocumentSchema]

    @property
    def name(self) -> str:
        return self.profile.name

    def person(self, name: str) -> Person | None:
        wanted = name.strip().casefold()
        return next((p for p in self.profile.people if p.name.casefold() == wanted), None)

    def people_with_role(self, role: str) -> list[Person]:
        return [p for p in self.profile.people if role in p.roles]

    def chunks(self) -> list[HandbookChunk]:
        return [chunk for doc in self.handbook for chunk in doc.chunks]

    def web_connectors(self) -> list[ConnectorSpec]:
        return [c for c in self.connectors.values() if c.kind == "web_app"]

    def connector_for_origin(self, origin: str) -> ConnectorSpec | None:
        return connector_for_origin(self.connectors, origin)

    def filesystem_root(self, connector: ConnectorSpec) -> Path:
        return resolve_path(connector.root or "")

    def problems(self) -> list[str]:
        """Cross-reference checks that pydantic cannot do on one file alone."""
        issues: list[str] = []
        roles = {role for p in self.profile.people for role in p.roles}
        if not self.person(self.profile.default_requester):
            issues.append(f"default_requester {self.profile.default_requester!r} is not in people")
        for rule in self.policies.rules:
            connector = self.connectors.get(rule.match.connector)
            if connector is None:
                issues.append(f"policy {rule.id}: unknown connector {rule.match.connector!r}")
                continue
            action = connector.action(rule.match.action)
            if action is None:
                issues.append(f"policy {rule.id}: connector {connector.id} has no action {rule.match.action!r}")
                continue
            for cond in rule.when:
                if cond.field not in action.fields:
                    issues.append(f"policy {rule.id}: action {action.name} declares no field {cond.field!r}")
            if rule.approver_role and rule.approver_role not in roles:
                issues.append(f"policy {rule.id}: nobody holds approver role {rule.approver_role!r}")
        for connector in self.connectors.values():
            for action in connector.actions:
                known = set(action.fields) | set(template_params(action.path))
                for key in action.idempotency_key:
                    if key not in known:
                        issues.append(f"{connector.id}.{action.name}: idempotency_key field {key!r} not declared")
                if action.reconcile:
                    if not connector.api or not connector.api.endpoint(action.reconcile.api):
                        issues.append(f"{connector.id}.{action.name}: reconcile api {action.reconcile.api!r} is not a declared endpoint")
                    templates = list(action.reconcile.params.values()) + list((action.reconcile.match or {}).values())
                    for name in {p for t in templates for p in template_params(t)}:
                        if name not in known:
                            issues.append(f"{connector.id}.{action.name}: reconcile template uses unknown field {name!r}")
                if action.reconcile and not action.idempotency_key:
                    issues.append(f"{connector.id}.{action.name}: reconcile is declared but idempotency_key is empty")
        return issues


def load_company(company_dir: Path | None = None) -> Company:
    company_dir = company_dir or get_settings().company_dir
    if not company_dir.exists():
        raise FileNotFoundError(f"company directory not found: {company_dir}")
    profile = CompanyProfile.model_validate(yaml.safe_load((company_dir / "company.yaml").read_text(encoding="utf-8")))
    handbook = [parse_handbook(p) for p in sorted((company_dir / "handbook").glob("*.md"))]
    schemas = {}
    for path in sorted((company_dir / "schemas").glob("*.json")):
        schema = DocumentSchema.model_validate(json.loads(path.read_text(encoding="utf-8")))
        schemas[schema.name] = schema
    return Company(
        dir=company_dir,
        profile=profile,
        handbook=handbook,
        policies=load_policies(company_dir / "policies.yaml"),
        connectors=load_connectors(company_dir),
        schemas=schemas,
    )
