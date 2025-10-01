"""First-seen event management functions."""
from typing import Any
import json

from events import group, message
import store
import crypto

def project_ids(first_seen_ids: list[str], db: Any) -> list[list[str | None]]:
    """Since `first_seen` is the event that triggers projection, this is the central function for projection."""
    """It calls the necessary project functions in other modules for the given event types."""
    projected_ids = [project(id, db) for id in first_seen_ids]
    return projected_ids


def project(first_seen_id: str, db: Any) -> list[str | None]:
    """If we have a first_seen event id both are already in the store"""
    """Get the first_seen event by id from the store and extract the event id it references"""

    first_seen_event_blob = store.get(first_seen_id, db)
    unwrapped_first_seen_event = crypto.unwrap(first_seen_event_blob, db)
    first_seen_event = json.loads(unwrapped_first_seen_event)
    referenced_event_id = first_seen_event['ref_id']

    # Extract peer and timestamp from first_seen event
    seen_by_peer_id = first_seen_event['created_by']  # Who saw this event
    received_at = first_seen_event['created_at']  # When they saw it

    """Update our tables mapping first_seen events to the events they reference"""
    db.execute(
        "INSERT INTO first_seen (first_seen_id, event_id, seen_by_peer_id, received_at) VALUES (?, ?, ?, ?)",
        (first_seen_id, referenced_event_id, seen_by_peer_id, received_at)
    )

    """Call the appropriate project function based on the event type"""
    event = store.get(referenced_event_id, db)
    unwrapped_event = crypto.unwrap(event, db)
    event_data = json.loads(unwrapped_event)
    """Project based on event type for all in events/ directory except first_seen"""
    event_type = event_data['type']  # e.g., 'message', 'group', etc.
    if event_type == 'message':
        projected_id = message.project(referenced_event_id, db, seen_by_peer_id, received_at)
    elif event_type == 'group':
        projected_id = group.project(referenced_event_id, db, seen_by_peer_id, received_at)
    else:
        projected_id = None  # No projection needed for this event type
    return [projected_id, first_seen_id]


def create(event_id: str, creator: str, t_ms: int, db: Any, return_dupes: bool) -> str:
    """Create a first_seen event for the given event_id and return the first_seen_id."""
    # TODO: implement - create a first_seen event blob, store it
    return ""