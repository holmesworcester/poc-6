"""Admin event type (shareable, plaintext) - grants admin status to a user.

This is a first-class event type, NOT a group. Admin status is granted by:
- Bootstrap: signed_by=network_id (verified with network_pubkey)
- Ongoing: signed_by=peer_shared_id (verified with peer.pubkey + admin_grant chain)
"""

# Registry metadata
EVENT_TYPE = 'admin'
SHAREABLE = True  # Admin grants sync across network
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp


EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'network': {
            'source': 'table',
            'table': 'networks',
            'key': 'network_id',
            'fields': ['network_id'],
        },
    },
    'optional': {
        'admin_grant': {
            'source': 'table',
            'table': 'admins',
            'key': 'admin_id',
            'key_from': 'admin_grant',
            'fields': ['admin_id', 'user_id'],
            'required_if_present': True,
        },
    },
    'cascade_on_delete': [],
}

log = logging.getLogger(__name__)



def _wire_shadow_admin(user_id: str, network_id: str, admin_grant: str | None) -> None:
    """Validate admin fields against the fixed-size wire payload layout."""
    admin_grant_bytes = crypto.b64decode(admin_grant) if admin_grant else None
    plaintext = wire_format.encode_admin_plaintext(
        user_id=crypto.b64decode(user_id),
        network_id=crypto.b64decode(network_id),
        admin_grant_id=admin_grant_bytes,
    )
    decoded = wire_format.decode_admin_plaintext(plaintext)
    if decoded["user_id"] != crypto.b64decode(user_id):
        raise ValueError("wire shadow decode user_id mismatch")

def create(
    user_id: str,
    network_id: str,
    signed_by: str,
    signer_private_key: bytes,
    t_ms: int,
    peer_id: str,
    db: Any,
    admin_grant: str | None = None
) -> str:
    """Create an admin event granting admin status to a user.

    Args:
        user_id: The user being granted admin
        network_id: The network this admin grant is for
        signed_by: Either network_id (bootstrap) or peer_shared_id (ongoing)
        signer_private_key: Private key corresponding to signed_by
        t_ms: Timestamp
        peer_id: Local peer ID (for recording)
        db: Database connection
        admin_grant: Prior admin_id for authorization chain (None for bootstrap)

    Returns:
        admin_id: The ID of the created admin event
    """
    _wire_shadow_admin(user_id, network_id, admin_grant)

    blob = wire_format.encode_admin_wire_event(
        user_id_b64=user_id,
        network_id_b64=network_id,
        signed_by_b64=signed_by,
        signer_type='network' if signed_by == network_id else 'peer_shared',
        admin_grant_id_b64=admin_grant,
        created_at_ms=t_ms,
        private_key=signer_private_key,
    )
    admin_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"admin.create() created admin grant: admin_id={admin_id[:20]}..., "
             f"user_id={user_id[:20]}..., network_id={network_id[:20]}..., "
             f"signed_by={signed_by[:20]}...")

    return admin_id


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for admin events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'admin':
        return ProjectorResult(writes=tuple(), valid_event=False)

    signed_by = event_data.get('signed_by')
    network_id = event_data.get('network_id')
    user_id = event_data.get('user_id')
    admin_grant = event_data.get('admin_grant')

    if not signed_by or not network_id or not user_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_admin(user_id, network_id, admin_grant)

    signer = ctx.signer or {}
    signer_type = signer.get('type')
    is_bootstrap = signed_by == network_id

    if is_bootstrap:
        if signer_type and signer_type != 'network':
            return ProjectorResult(writes=tuple(), valid_event=False)
    else:
        if not admin_grant:
            return ProjectorResult(writes=tuple(), valid_event=False)
        if signer_type and signer_type != 'peer_shared':
            return ProjectorResult(writes=tuple(), valid_event=False)
        signer_user_id = signer.get('user_id')
        if not signer_user_id:
            return ProjectorResult(writes=tuple(), valid_event=False)
        grant_row = ctx.deps.get('admin_grant')
        if not grant_row or grant_row.get('user_id') != signer_user_id:
            return ProjectorResult(writes=tuple(), valid_event=False)

    writes = [
        WriteOp(
            op='insert',
            table='admins',
            values={
                'admin_id': ctx.event_id,
                'network_id': network_id,
                'user_id': user_id,
                'signed_by': signed_by,
                'admin_grant': admin_grant,
                'created_at': event_data.get('created_at'),
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    ]

    if is_bootstrap:
        writes.append(
            WriteOp(
                op='update',
                table='networks',
                values={'creator_user_id': user_id},
                where={
                    'network_id': network_id,
                    'recorded_by': ctx.recorded_by,
                    'creator_user_id': '',
                },
            )
        )

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def is_user_admin(user_id: str, network_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a user has admin status in a network.

    Args:
        user_id: The user to check
        network_id: The network to check admin status for
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if user is an admin, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    admin_row = safedb.query_one(
        "SELECT 1 FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ?",
        (user_id, network_id, recorded_by)
    )

    return admin_row is not None


def my_grant(user_id: str, network_id: str, recorded_by: str, db: Any) -> str | None:
    """Get the admin_id that granted admin to a user.

    Used for creating admin_grant chain when granting admin to others.

    Args:
        user_id: The admin user
        network_id: The network
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        admin_id if found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    admin_row = safedb.query_one(
        "SELECT admin_id FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ?",
        (user_id, network_id, recorded_by)
    )

    return admin_row['admin_id'] if admin_row else None
