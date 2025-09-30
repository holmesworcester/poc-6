"""
Event utilities for the minimal event model.

Event shape: { id: str, type: str, ...protocolFields }

Validation rules (current behavior):
- id and type are required, non-empty strings
- If a handler JSON schema exists for this type, validate protocol fields
  (the event minus id/type) against that schema.
- If no schema exists, proceed (log-only). To make schemas required,
  set REQUIRE_EVENT_SCHEMA=1 in the environment.
"""
from __future__ import annotations

import os
from typing import Dict, Any

from core.handler_discovery import load_handler_config
from core.schema_validator import validate_against_schema


def _protocol_fields(event: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in event.items() if k not in ("id", "type")}


def validate_event(event: Dict[str, Any], handler_base: str = "handlers") -> None:
    """Validate an event against core requirements and optional handler schema.

    - Ensures event has id and type (non-empty strings)
    - Validates protocol fields against the handler's schema if available
    - Honors REQUIRE_EVENT_SCHEMA=1 to enforce schema presence
    """
    if not isinstance(event, dict):
        raise ValueError("Event must be a dict")

    ev_id = event.get("id")
    ev_type = event.get("type")
    if not isinstance(ev_id, str) or not ev_id.strip():
        raise ValueError("Event.id must be a non-empty string")
    if not isinstance(ev_type, str) or not ev_type.strip():
        raise ValueError("Event.type must be a non-empty string")

    # Load handler config to find schema
    config = load_handler_config(ev_type, handler_base)
    schema = None
    if isinstance(config, dict):
        schema = config.get("schema")

    require_schema = os.environ.get("REQUIRE_EVENT_SCHEMA") in ("1", "true", "True")
    if schema is None:
        if require_schema:
            raise ValueError(f"Missing required schema for event type '{ev_type}'")
        # Best-effort compatibility: allow when schema absent
        return

    # Validate protocol fields against schema
    ok, err = validate_against_schema(_protocol_fields(event), schema)
    if not ok:
        raise ValueError(f"Event validation failed for type '{ev_type}': {err}")

