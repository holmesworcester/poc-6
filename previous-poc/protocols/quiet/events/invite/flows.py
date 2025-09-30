"""
Flows for invite operations.
"""
from __future__ import annotations

import base64
import json
import time
import secrets
import hashlib
from typing import Any, Dict

from core.crypto import kdf, generate_secret, hash as blake2b, get_root_secret_id
from typing import Optional
from core.flows import FlowCtx, flow_op


@flow_op()  # Registers as 'invite.create'
def create(params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = FlowCtx.from_params(params)

    network_id = params.get('network_id', '')
    group_id = params.get('group_id', '')
    peer_id = params.get('peer_id', '')
    try:
        print(f"[invite.create] params keys: {sorted(list(params.keys()))}")
        print(f"[invite.create] received network_id={network_id}, group_id={group_id}, peer_id={peer_id}")
    except Exception:
        pass
    if not peer_id:
        raise ValueError('peer_id is required for create_invite')

    # Generate invite secret and derive pubkey (store only pubkey)
    invite_secret = secrets.token_urlsafe(24)  # shorter but sufficient entropy # TODO: make sure this is a real key
    invite_salt = hashlib.sha256(b"quiet_invite_kdf_v1").digest()[:16]
    derived_key = kdf(invite_secret.encode(), salt=invite_salt)
    invite_pubkey = derived_key.hex()

    # Choose an address owner for initial contact: for now, pick the inviter's own address
    addr_owner_peer = peer_id

    # Include prekey for the address owner to bootstrap KEM-sealed sync_request from joiner
    prekey_info: Optional[dict] = None
    try:
        prekey_info = ctx.query('prekey.get_active_for_peer', {
            'peer_id': addr_owner_peer,
            'network_id': network_id,
            'prefer_newest': True,
            'min_expires_ms': 0,
        })
    except Exception:
        prekey_info = None

    # Fetch the address (ip, port) for the selected owner
    addr_ip, addr_port = '127.0.0.1', 8080
    try:
        latest = ctx.query('address.get_latest_for_peer', {'peer_id': addr_owner_peer, 'network_id': network_id})
        if isinstance(latest, dict):
            addr_ip = latest.get('ip', addr_ip)
            addr_port = latest.get('port', addr_port)
    except Exception:
        pass

    # Create an invite-scoped key_secret and expose its id to the group
    invite_key_secret = generate_secret()
    # Persist local-only key_secret so we can encrypt with it if needed and capture its id
    root_id = get_root_secret_id()

    invite_key_secret_id = ctx.emit_event({
        'event_type': 'key_secret',
        'event_plaintext': {
            'type': 'key_secret',
            'unsealed_secret': invite_key_secret.hex(),
            'created_at': int(time.time() * 1000),
        },
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })

    # Create a transit secret for DEM transit bootstrap (shared via invite)
    transit_secret = generate_secret()
    transit_secret_id = blake2b(transit_secret, size=32).hex()

    # Persist transit secret as key_secret so network_gate can index it
    # The key_secret_id will be derived as blake2b(secret, size=16), but we need the
    # transit_secret_id (32-byte) for network indexing
    ctx.emit_event({
        'event_type': 'key_secret',
        'event_plaintext': {
            'type': 'key_secret',
            'unsealed_secret': transit_secret.hex(),
            'created_at': int(time.time() * 1000),
        },
        'network_id': network_id,  # Provide network context for network_gate
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })

    # Emit invite event, including a public hint of the invite key id
    invite_env = {
        'event_type': 'invite',
        'event_plaintext': {
            'type': 'invite',
            'invite_pubkey': invite_pubkey,
            'network_id': network_id,
            'group_id': group_id,
            'inviter_id': peer_id,
            'invite_key_secret_id': invite_key_secret_id,
            'transit_secret_id': transit_secret_id,
            'created_at': int(time.time() * 1000),
            'created_by': peer_id,
        },
        'peer_id': peer_id,
        'deps': [group_id] + ([params.get('peer_secret_id')] if isinstance(params.get('peer_secret_id'), str) and params.get('peer_secret_id') else []),
        'self_created': True,
    }
    invite_id = ctx.emit_event(invite_env)

    # Build compact, urlsafe link payload
    invite_data = {
        'invite_secret': invite_secret,
        'network_id': network_id,
        'group_id': group_id,
        # Address owner prekey hints for sealing sync_request
        **({'prekey_secret_id': prekey_info.get('prekey_id')} if prekey_info else {}),
        **({'prekey_public': prekey_info.get('public_key')} if prekey_info else {}),
        # Invite-scoped key for initial event-layer encryption (joiner can derive id)
        'invite_key_secret': invite_key_secret.hex(),
        # Transit DEM secret for initial transit until prekeys sync
        'transit_secret_id': transit_secret_id,
        'transit_secret': transit_secret.hex(),
        # Address for reachability/join
        'ip': addr_ip,
        'port': addr_port,
    }
    # Debug: surface what we are encoding in the link
    try:
        print(f"[invite.create] Building invite link with keys: {sorted(list(invite_data.keys()))}")
        if not network_id or not group_id:
            print(f"[invite.create] WARNING: network_id/group_id empty in invite_data: network_id={network_id}, group_id={group_id}")
    except Exception:
        pass

    invite_json = json.dumps(invite_data, separators=(',', ':'), sort_keys=True)
    invite_code = base64.urlsafe_b64encode(invite_json.encode()).decode().rstrip('=')
    invite_link = f"quiet://invite/{invite_code}"

    return {
        'ids': {'invite': invite_id},
        'data': {
            'invite_link': invite_link,
            'invite_code': invite_code,
            'invite_id': invite_id,
            'network_id': network_id,
            'group_id': group_id,
            'prekey_owner_peer_id': addr_owner_peer,
            'prekey_secret_id': prekey_info.get('prekey_id') if prekey_info else None,
            'invite_key_secret_id': invite_key_secret_id,
        },
    }
