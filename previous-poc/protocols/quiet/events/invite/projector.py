"""
Projector for invite events.
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    No-op projector for invite: we no longer maintain an invites table.
    Invite events are stored in the event store and returned to the caller
    for display; the link itself is not persisted.
    """
    return []
