"""Dependency Resolver for Pure Functional Projectors.

Sits between event source and projectors:
1. Given an event, determines what dependencies it needs
2. Fetches those dependencies from the event store (NOT projections)
3. Checks all deps are valid/unblocked
4. Assembles the input dict for the projector

Key principle: transitive validity is assumed.
If event B depends on event A, and event C depends on event B,
then when we project C, we only need to check that B is valid.
We don't need to re-fetch A because B being valid implies A is valid.
"""

from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def resolve_message(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> dict | None:
    """Resolve dependencies for a message event.

    Dependencies:
    - channel: the channel this message belongs to (via event_data.channel_id)
    - deletion: optional, if a deletion event exists

    Returns input dict for the projector, or None if cannot resolve.
    """
    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get and unwrap the message event
    event_blob = store.get(event_id, unsafedb)
    if not event_blob:
        log.warning(f"resolve_message: blob not found for {event_id}")
        return None

    unwrapped, _ = crypto.unwrap(event_blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"resolve_message: unwrap failed for {event_id}")
        return None

    event_data = crypto.parse_json(unwrapped)

    # Verify signature
    from events.identity import peer_shared
    signed_by = event_data.get("signed_by")
    public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"resolve_message: signature verification failed for {event_id}")
        return None

    # Extract key_id from blob
    key_id_bytes = event_blob[:crypto.ID_SIZE]
    key_id = crypto.b64encode(key_id_bytes)

    # Resolve channel dependency
    channel_id = event_data.get("channel_id")
    channel_dep = resolve_event(channel_id, recorded_by, db)

    # Check for deletion (optional)
    deletion_dep = None
    deletion_row = safedb.query_one(
        "SELECT deleted_by FROM message_deletions WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (event_id, recorded_by)
    )
    if deletion_row:
        # Pre-compute if deletion is valid
        from events.content import message_deletion
        is_valid = message_deletion.validate(event_id, deletion_row["deleted_by"], recorded_by, db)
        deletion_dep = {
            "deleted_by": deletion_row["deleted_by"],
            "is_valid": is_valid
        }

    return {
        "event_id": event_id,
        "event_data": event_data,
        "key_id": key_id,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "channel": channel_dep,
            "deletion": deletion_dep
        }
    }


def resolve_channel(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> dict | None:
    """Resolve dependencies for a channel event.

    Dependencies:
    - signer_user: user who signed this event (looked up via linked_peers)
    - admin_grant: if event has admin_grant field, the admin event
    """
    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get and unwrap the channel event
    event_blob = store.get(event_id, unsafedb)
    if not event_blob:
        log.warning(f"resolve_channel: blob not found for {event_id}")
        return None

    unwrapped, _ = crypto.unwrap(event_blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"resolve_channel: unwrap failed for {event_id}")
        return None

    event_data = crypto.parse_json(unwrapped)

    # Verify signature
    from events.identity import peer_shared
    signed_by = event_data.get("signed_by")
    public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"resolve_channel: signature verification failed for {event_id}")
        return None

    # Resolve signer_user (from linked_peers - this IS from projection but it's identity data)
    signer_user_dep = None
    linked_row = safedb.query_one(
        "SELECT user_id FROM linked_peers WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (signed_by, recorded_by)
    )
    if linked_row:
        signer_user_dep = {
            "user_id": linked_row["user_id"],
            "peer_id": signed_by
        }

    # Resolve admin_grant if present
    admin_grant_dep = None
    admin_grant_id = event_data.get("admin_grant")
    if admin_grant_id:
        grant_row = safedb.query_one(
            "SELECT user_id FROM admins WHERE admin_id = ? AND recorded_by = ? LIMIT 1",
            (admin_grant_id, recorded_by)
        )
        if grant_row:
            admin_grant_dep = {
                "event_id": admin_grant_id,
                "user_id": grant_row["user_id"]
            }

    return {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "signer_user": signer_user_dep,
            "admin_grant": admin_grant_dep
        }
    }


def resolve_group_member(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> dict | None:
    """Resolve dependencies for a group_member event.

    Dependencies:
    - group: the group this member is being added to
    - user: the user being added
    - adder_user: user who added this member (via linked_peers)
    - admin_grant: if event has admin_grant field, the admin event
    """
    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get and unwrap the group_member event
    event_blob = store.get(event_id, unsafedb)
    if not event_blob:
        log.warning(f"resolve_group_member: blob not found for {event_id}")
        return None

    unwrapped, _ = crypto.unwrap(event_blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"resolve_group_member: unwrap failed for {event_id}")
        return None

    event_data = crypto.parse_json(unwrapped)

    # Verify signature
    from events.identity import peer_shared
    added_by = event_data.get("added_by")
    if added_by:
        public_key = peer_shared.get_public_key(added_by, recorded_by, db)
        if not crypto.verify_event(event_data, public_key):
            log.warning(f"resolve_group_member: signature verification failed for {event_id}")
            return None

    # Resolve group dependency
    group_id = event_data.get("group_id")
    group_dep = resolve_event(group_id, recorded_by, db)

    # Resolve user dependency
    user_id = event_data.get("user_id")
    user_dep = resolve_event(user_id, recorded_by, db)

    # Resolve adder_user (from linked_peers)
    adder_user_dep = None
    linked_row = safedb.query_one(
        "SELECT user_id FROM linked_peers WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (added_by, recorded_by)
    )
    if linked_row:
        adder_user_dep = {
            "user_id": linked_row["user_id"],
            "peer_id": added_by
        }

    # Resolve admin_grant if present
    admin_grant_dep = None
    admin_grant_id = event_data.get("admin_grant")
    if admin_grant_id:
        grant_row = safedb.query_one(
            "SELECT user_id FROM admins WHERE admin_id = ? AND recorded_by = ? LIMIT 1",
            (admin_grant_id, recorded_by)
        )
        if grant_row:
            admin_grant_dep = {
                "event_id": admin_grant_id,
                "user_id": grant_row["user_id"]
            }

    return {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "group": group_dep,
            "user": user_dep,
            "adder_user": adder_user_dep,
            "admin_grant": admin_grant_dep
        }
    }


def resolve_event(event_id: str, recorded_by: str, db: Any) -> dict | None:
    """Generic event resolver - just gets event_data.

    Used for simple dependencies where we just need to verify the event exists
    and get its payload.
    """
    unsafedb = create_unsafe_db(db)

    event_blob = store.get(event_id, unsafedb)
    if not event_blob:
        return None

    unwrapped, _ = crypto.unwrap(event_blob, recorded_by, db)
    if not unwrapped:
        return None

    event_data = crypto.parse_json(unwrapped)

    return {
        "event_id": event_id,
        "event_data": event_data
    }
