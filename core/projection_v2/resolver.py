"""Resolver scaffolding for projection v2."""
from __future__ import annotations

from typing import Any

from .types import ResolveResult


def resolve_event(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> ResolveResult:
    """Decode, verify, and resolve dependencies for a single event.

    Implementation is provided by v2 core workstream.
    """
    raise NotImplementedError
