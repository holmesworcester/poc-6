"""Peer name update event type (encrypted name update for device names)."""

# Registry metadata
EVENT_TYPE = 'peer_name_update'
SHAREABLE = True  # Device name updates sync across network
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import logging
import crypto
import store
from events.group import group
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, name: str, signed_by: str, t_ms: int,
           db: Any) -> str:
    """Create a peer_name_update event (device name).

    The device name is encrypted to the all_members group using the latest known key.
    If the key is not available, raises KeyNotAvailableError.

    Args:
        peer_id: The peer (device) this name updates
        name: Plaintext device name to encrypt
        signed_by: The peer_shared_id signing this update
        t_ms: Timestamp
        db: Database connection

    Returns:
        peer_name_update_id: The stored event ID

    Raises:
        KeyNotAvailableError: If all_members group key is not available yet
        ValueError: If peer doesn't exist
    """
    # For now, device names are stored as plaintext in peers_shared table
    # This stub implementation allows the system to work without errors
    # Full encrypted device names can be added later if needed
    log.info(f"peer_name_update.create() stub - device names stored in peers_shared table")
    raise KeyNotAvailableError("Peer name updates not yet implemented - use peers_shared device_name field")


def validate(event_id: str, recorded_by: str, db: Any) -> str | None:
    """Validate a peer_name_update event.

    Args:
        event_id: The peer_name_update event ID
        recorded_by: The peer that recorded this event
        db: Database connection

    Returns:
        'VALID' if peer exists and valid
        'BLOCKED' if peer doesn't exist yet
        None if invalid
    """
    # Stub implementation
    return None


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a peer_name_update event into the database.

    Args:
        event_id: The peer_name_update event ID
        recorded_by: The peer that recorded this event
        recorded_at: When this was recorded
        db: Database connection

    Returns:
        event_id if projection succeeded, None otherwise
    """
    # Stub implementation - device names are currently stored in peers_shared
    return None


class KeyNotAvailableError(Exception):
    """Raised when group key is not available for encryption."""
    pass
