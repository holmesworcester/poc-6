"""Batch projection engine for projection v2."""
from __future__ import annotations

import logging
from typing import Any

from core.db import create_safe_db
from events import registry
from .apply import apply_writes
from .resolver import resolve_event
from .types import ResolverCache

log = logging.getLogger(__name__)

EventInput = tuple[str, str, dict[str, Any], str, int]


def _extract_potential_dep_ids(event_data: dict[str, Any]) -> list[str]:
    """Extract potential dependency IDs from event data for pre-fetching.

    Returns all string values from fields ending in '_id' or known reference fields.
    """
    dep_ids = []
    for key, value in event_data.items():
        if not isinstance(value, str) or len(value) < 10:
            continue
        # Fields ending in '_id' are potential deps
        if key.endswith('_id'):
            dep_ids.append(value)
        # Known reference fields
        elif key in ('signed_by', 'added_by', 'created_by', 'admin_grant'):
            dep_ids.append(value)
    return dep_ids


def project_batch(events: list[EventInput], db: Any, use_cache: bool = True) -> list[str | None]:
    """Project a batch of events using v2 pipeline.

    Each event item is (ref_id, event_type, event_data, recorded_by, recorded_at).
    Returns a list of projected ref_ids (None for block/reject).

    Args:
        events: List of events to project
        db: Database connection
        use_cache: Whether to use RETE-style caching (default True).
                   Set to False only for debugging or specific test scenarios.
    """
    if not events:
        return []

    # RETE optimization: Create shared cache for the batch
    cache = ResolverCache() if use_cache else None

    # RETE optimization: Pre-fetch valid_events status for all potential deps
    if cache:
        # Group events by recorded_by for efficient prefetching
        by_peer: dict[str, list[str]] = {}
        for ref_id, event_type, event_data, recorded_by, recorded_at in events:
            if recorded_by not in by_peer:
                by_peer[recorded_by] = []
            by_peer[recorded_by].extend(_extract_potential_dep_ids(event_data))

        # Pre-fetch valid_events for each peer
        for recorded_by, dep_ids in by_peer.items():
            if dep_ids:
                safedb = create_safe_db(db, recorded_by=recorded_by)
                cache.prefetch_valid_events(list(set(dep_ids)), recorded_by, safedb)

    results: list[str | None] = []
    for ref_id, event_type, event_data, recorded_by, recorded_at in events:
        resolve_result = resolve_event(
            ref_id,
            event_type,
            event_data,
            recorded_by,
            recorded_at,
            db,
            cache,
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

        # RETE optimization: Mark successfully projected event as valid in cache
        # This helps subsequent events in the batch that depend on this one
        if cache:
            cache.set_event_valid(ref_id, recorded_by, True)

        results.append(ref_id)

    # Log cache stats for debugging
    if cache and log.isEnabledFor(logging.DEBUG):
        log.debug(f"project_batch: {cache.stats()}")

    return results
