"""
Projector for key_secret events.

Local-only key_secret events are already stored in the event store and
don’t need table projections.
"""
from __future__ import annotations

from typing import List, Dict, Any


def project(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """No-op projector for key_secret events."""
    return []

