"""Seen event management functions."""
from typing import Any

import group
import message from events/message
import store
import crypto
import json

def project_ids(seen_ids: list[str], db: Any) -> None:
    """Since `seen` is the event that triggers proejction, this is the central function for projection."""
    """It calls the necessary project functions in other modules for the given event types."""
    projected_ids = seen_ids.map(lambda id: project_seen_event(id, db))
    return projected_ids

def project_seen_event(seen_id: str, db: Any) -> [str]:
    """If we have a seen event id both are already in the store"""
    """Get the seen event by id from the store and extract the event id it references"""
    seen_event_blob = store.get(seen_id, db)
    unwrapped_seen_event = crypto.unwrap(seen_event_blob, db)
    seen_event = json.parse(unwrapped_seen_event)
    referenced_event_id = seen_event['ref_id']

    """Update our tables mapping seen events to the events they reference"""
    db.execute("INSERT INTO seen_events (seen_id, event_id) VALUES (?, ?)", (seen_id, referenced_event_id))
    """Call the appropriate project function based on the event type"""
    event = store.get(referenced_event_id, db)
    unwrapped_event = crypto.unwrap(event, db)
    event_data = json.parse(unwrapped_event)
    """Project based on event type for all in events/ directory except seen"""
    event_type = event_data['type'] # e.g., 'message', 'group', etc.
    if event_type == 'message':
        projected_id = message.project([referenced_event_id], db)
    elif event_type == 'group':
        projected_id = group.project([referenced_event_id], db)
    else:
        projected_id = None # No projection needed for this event type
    return [projected_id, seen_id] # is this a problem if projected_id is null?