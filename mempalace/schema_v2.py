"""Phase 2 memory schema validation helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


V2_REQUIRED_FIELDS = {
    "schema_version",
    "memory_type",
    "source_system",
    "source_id",
    "wing",
    "room",
    "created_at",
    "event_time",
}
LEGACY_REQUIRED_FIELDS = {"schema_version", "stamp_source", "stamped_at"}

VALID_SCHEMA_VERSIONS = {"v2", "legacy-import-v1"}
VALID_MEMORY_TYPES = {
    "cron_summary",
    "llm_output",
    "diary_daily",
    "raven_feedback",
    "raven_preference",
    "kg_event",
    "decision",
    "incident",
    "legacy_import",
    "claude_code_memory",
}
VALID_SOURCE_SYSTEMS = {
    "daemon",
    "claude-code",
    "openclaw-notes",
    "legacy-memory-service",
    "raven-manual",
}
VALID_PRIVACY_SCOPES = {"public", "agent-wide", "specific-agents", "raven-only"}
VALID_AGENTS = {"iris", "dev", "quant", "ops", "meridian", "nova", "doctor"}


@dataclass(slots=True)
class MemorySchema:
    schema_version: str
    memory_type: str | None = None
    source_system: str | None = None
    source_id: str | None = None
    wing: str | None = None
    room: str | None = None
    created_at: str | None = None
    event_time: str | None = None
    importance: int = 3
    privacy_scope: str = "agent-wide"
    readable_by: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    source_device: str | None = None
    source_path: str | None = None
    source_content_hash: str | None = None
    source_deleted_at: str | None = None
    stamp_source: str | None = None
    stamped_at: str | None = None


def enrich(doc: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a copy of *doc* with Phase 2 default metadata filled."""

    enriched = deepcopy(defaults or {})
    enriched.update(deepcopy(doc))
    enriched.setdefault("importance", 3)
    enriched.setdefault("privacy_scope", "agent-wide")
    enriched.setdefault("readable_by", [])
    enriched.setdefault("provenance", {})
    enriched.setdefault("links", [])
    enriched.setdefault("superseded_by", None)
    return enriched


def validate(doc: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a v2 or legacy-import-v1 memory metadata document."""

    errors: list[str] = []
    if not isinstance(doc, dict):
        return False, ["doc must be a dict"]

    schema_version = doc.get("schema_version")
    if schema_version not in VALID_SCHEMA_VERSIONS:
        errors.append("schema_version must be one of: legacy-import-v1, v2")
        return False, errors

    required = V2_REQUIRED_FIELDS if schema_version == "v2" else LEGACY_REQUIRED_FIELDS
    for field_name in sorted(required):
        if doc.get(field_name) in (None, ""):
            errors.append(f"{field_name} is required for schema_version={schema_version}")

    if schema_version == "legacy-import-v1":
        stamped_at = doc.get("stamped_at")
        if stamped_at and not _is_iso_datetime(stamped_at):
            errors.append("stamped_at must be ISO-8601")
        return not errors, errors

    memory_type = doc.get("memory_type")
    if memory_type and memory_type not in VALID_MEMORY_TYPES:
        errors.append(f"memory_type is invalid: {memory_type}")

    source_system = doc.get("source_system")
    if source_system and source_system not in VALID_SOURCE_SYSTEMS:
        errors.append(f"source_system is invalid: {source_system}")

    importance = doc.get("importance", 3)
    if not isinstance(importance, int) or not 1 <= importance <= 5:
        errors.append("importance must be an integer from 1 to 5")

    privacy_scope = doc.get("privacy_scope", "agent-wide")
    if privacy_scope not in VALID_PRIVACY_SCOPES:
        errors.append(f"privacy_scope is invalid: {privacy_scope}")

    readable_by = doc.get("readable_by", [])
    if readable_by in ("all", None):
        readable_by = []
    if not isinstance(readable_by, list):
        errors.append("readable_by must be a list")
    elif privacy_scope == "specific-agents":
        unknown = sorted(set(readable_by) - VALID_AGENTS)
        if not readable_by:
            errors.append("readable_by is required when privacy_scope=specific-agents")
        if unknown:
            errors.append(f"readable_by contains unknown agents: {', '.join(unknown)}")

    for list_field in ("links",):
        if list_field in doc and not isinstance(doc[list_field], list):
            errors.append(f"{list_field} must be a list")

    for dict_field in ("provenance",):
        if dict_field in doc and not isinstance(doc[dict_field], dict):
            errors.append(f"{dict_field} must be a dict")

    for time_field in ("created_at", "event_time", "source_deleted_at"):
        value = doc.get(time_field)
        if value and not _is_iso_datetime(value):
            errors.append(f"{time_field} must be ISO-8601")

    return not errors, errors


def _is_iso_datetime(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
