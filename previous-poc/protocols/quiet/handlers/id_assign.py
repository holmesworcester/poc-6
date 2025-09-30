"""
ID assignment handler.

Single source of truth for generating deterministic event IDs.

Invariant:
- All events are encrypted and IDs are derived from the same input: `event_blob`.

Responsibilities:
- Assign `event_id` strictly from `event_blob` (header + nonce + ciphertext)
- Never assign IDs from plaintext
- Set `write_to_store` flag for events that should be stored immediately
- Never generate functional IDs for secrets (key_secret_id, peer_secret_id, etc.)

Notes:
- Local-only secret events may still set `write_to_store`, but their IDs are
  not generated here.
"""

from typing import Any, List
import sqlite3
import hashlib
from core.handlers import Handler


def _should_assign_event_id(envelope: dict[str, Any]) -> bool:
    """Check if this envelope needs an event_id assigned.

    Only assign when an `event_blob` is present and no `event_id` yet.
    """
    if envelope.get('event_id'):
        return False
    return bool(envelope.get('event_blob'))


def _assign_event_id_from_blob(envelope: dict[str, Any]) -> str:
    """Generate event_id from entire event_blob (hint + ciphertext)."""
    blob = envelope.get('event_blob')
    if not blob:
        raise ValueError("No event_blob available for ID generation")

    if isinstance(blob, str):
        blob = blob.encode('utf-8')

    # Always hash the entire blob for consistency
    return hashlib.blake2b(bytes(blob), digest_size=16).hexdigest()


# Plaintext-based ID assignment removed by design: all events are encrypted.


def _should_store_immediately(envelope: dict[str, Any]) -> bool:
    """Determine if this event should be marked for immediate storage."""
    event_type = envelope.get('event_type') or (envelope.get('event_plaintext') or {}).get('type')

    # Local-only secret events are always stored immediately
    if event_type in ('peer_secret', 'key_secret', 'prekey_secret', 'transit_secret'):
        return True

    # Encrypted events with an event_blob are ready for storage
    if envelope.get('event_blob'):
        return True

    # Public events like 'peer' or 'sync_request' are encrypted in this design,
    # but keep permissive behavior to ease tests and flows.
    if event_type in ('peer', 'sync_request'):
        return True

    return False


class IdAssignHandler(Handler):
    @property
    def name(self) -> str:
        return "id_assign"

    def filter(self, envelope: dict[str, Any]) -> bool:
        """Filter for any envelope that needs an event_id assigned."""
        try:
            return _should_assign_event_id(envelope)
        except Exception:
            return False

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Assign event_id and storage flags to the envelope."""
        try:
            # Generate from event_blob only (encrypted events - header + nonce + ciphertext)
            if envelope.get('event_blob'):
                event_id = _assign_event_id_from_blob(envelope)
                envelope['event_id'] = event_id

            # Set storage flag based on event type and state
            if _should_store_immediately(envelope):
                envelope['write_to_store'] = True

        except Exception as e:
            # Best-effort: leave envelope unchanged on failure but log issue
            envelope['id_assign_error'] = str(e)

        return [envelope]
