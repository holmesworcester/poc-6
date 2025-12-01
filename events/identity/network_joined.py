"""Network joined event type - marks successful bootstrap with inviter."""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, peer_shared_id: str, inviter_peer_shared_id: str,
           t_ms: int, db: Any) -> str:
    """Create network_joined event after successful bootstrap.

    Args:
        peer_id: Joiner's peer_id
        peer_shared_id: Joiner's peer_shared_id
        inviter_peer_shared_id: Inviter's peer_shared_id
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_joined event ID
    """
    from events.identity import peer

    # Create event data
    event_data = {
        'type': 'network_joined',
        'peer_id': peer_id,
        'signed_by': peer_shared_id,
        'inviter_peer_shared_id': inviter_peer_shared_id,
        'created_at': t_ms
    }

    # Get joiner's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)
    if not private_key:
        log.error(f"network_joined.create() no private key for peer {peer_id[:20]}...")
        raise ValueError(f"No private key for peer {peer_id}")

    # Sign the event
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext
    canonical = crypto.canonicalize_json(signed_event)
    network_joined_id = store.event(canonical, peer_id, t_ms, db)

    log.info(f"network_joined.create() created event {network_joined_id[:20]}... for peer {peer_id[:20]}...")

    return network_joined_id


def project_event(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project network_joined event into network_joiners table."""
    from projectors import resolve, apply_result
    from projectors import network_joined as nj_projector

    input_dict = resolve("network_joined", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = nj_projector.project(input_dict)

    if not result.valid:
        log.warning(f"network_joined.project() failed: {result.reason}")
        return None

    apply_result(result, recorded_by, recorded_at, db)
    return event_id
