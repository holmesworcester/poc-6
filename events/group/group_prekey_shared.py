"""Prekey shared event type (shareable public prekey)."""
from typing import Any
import logging
import crypto
import store
from events.network import transit_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(prekey_id: str, peer_id: str, peer_shared_id: str,
           t_ms: int, db: Any,
           group_id: str | None = None, key_id: str | None = None,
           user_id: str | None = None,
           wrap_key_data: dict | None = None) -> str:
    """Create a shareable group_prekey_shared event from a local group prekey.

    Context must be either group-based (for user invites) or user-based (for link invites).
    Exactly one context type must be provided.

    Args:
        prekey_id: Local group_prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        group_id: Group context (for mode=user invites). Requires key_id.
        key_id: Key reference (for mode=user invites). Requires group_id.
        user_id: User context (for mode=link invites - device linking)
        wrap_key_data: Optional key dict for wrapping (used when network key not available yet)

    Returns:
        group_prekey_shared_id: The stored group_prekey_shared event ID

    Raises:
        ValueError: If neither group context nor user context is provided,
                    or if both are provided
    """
    # Validate context - must have exactly one type
    has_group_context = group_id is not None
    has_user_context = user_id is not None

    if not has_group_context and not has_user_context:
        raise ValueError("group_prekey_shared requires either group context (group_id, key_id) or user context (user_id)")
    if has_group_context and has_user_context:
        raise ValueError("group_prekey_shared cannot have both group context and user context")
    if has_group_context and key_id is None:
        raise ValueError("group context requires both group_id and key_id")

    log.info(f"group_prekey_shared.create() creating group_prekey_shared for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"prekey not found: {prekey_id}")

    prekey_data = crypto.parse_json(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Create shareable event (encrypted + signed)
    # Include group_prekey_id for linking back during projection
    event_data = {
        'type': 'group_prekey_shared',
        'group_prekey_id': prekey_id,
        'peer_id': peer_shared_id,
        'public_key': prekey_public_b64,
        'signed_by': peer_shared_id,
        'created_at': t_ms,
    }

    # Add context fields based on invite type:
    # - mode=user: group context (group_id, key_id)
    # - mode=link: user context (user_id)
    if has_group_context:
        event_data['group_id'] = group_id
        event_data['key_id'] = key_id
    else:
        event_data['user_id'] = user_id

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no inner encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    group_prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_prekey_shared.create() created group_prekey_shared_id={group_prekey_shared_id}")
    return group_prekey_shared_id



def project(group_prekey_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project group_prekey_shared event into group_prekeys_shared table."""
    from projectors import resolve, apply_result
    from projectors import group_prekey_shared as gpks_projector

    input_dict = resolve("group_prekey_shared", group_prekey_shared_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = gpks_projector.project(input_dict)

    if not result.valid:
        log.warning(f"group_prekey_shared.project() failed: {result.reason}")
        return None

    apply_result(result, recorded_by, recorded_at, db)
    return group_prekey_shared_id
