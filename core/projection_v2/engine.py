"""Batch projection engine for projection v2."""
from __future__ import annotations

import logging
from typing import Any

from events import registry
from .apply import apply_writes
from .resolver import resolve_event

log = logging.getLogger(__name__)

EventInput = tuple[str, str, dict[str, Any], str, int]


def project_batch(events: list[EventInput], db: Any) -> list[str | None]:
    """Project a batch of events using v2 pipeline.

    Each event item is (ref_id, event_type, event_data, recorded_by, recorded_at).
    Returns a list of projected ref_ids (None for block/reject).
    """
    results: list[str | None] = []
    for ref_id, event_type, event_data, recorded_by, recorded_at in events:
        resolve_result = resolve_event(
            ref_id,
            event_type,
            event_data,
            recorded_by,
            recorded_at,
            db,
        )
        if resolve_result.status != "ok":
            results.append(None)
            continue
        project_fn = registry.get_project_pure_fn(event_type)
        if not project_fn:
            log.warning(f"project_batch: missing project_pure for event_type={event_type}")
            results.append(None)
            continue
        projector_result = project_fn(resolve_result.ctx)
        if not projector_result or not projector_result.valid_event:
            results.append(None)
            continue
        apply_writes(projector_result, recorded_by, recorded_at, db)
        results.append(ref_id)
    return results
