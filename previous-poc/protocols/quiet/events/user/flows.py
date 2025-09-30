"""
Flows for user-related multi-step operations.

Flows are read-only orchestrations that can query, emit, and return.
They do not write to the DB directly; all state changes happen via emitted events.
"""
from __future__ import annotations

import base64
import hashlib
import json as json_module
from typing import Any, Dict

from core.crypto import kdf, hash as crypto_hash, generate_keypair
from core.flows import FlowCtx, flow_op
import time
import hashlib


@flow_op()  # Registers as 'user.join_as_user'
def join_as_user(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flow: create peer_secret (local-only), then peer, then user via invite.

    Expects the following execution context in params:
      - _db: sqlite3.Connection
      - _runner: PipelineRunner
      - _protocol_dir: str
      - _request_id: str

    Returns ids and minimal data; the command wrapper may still use a response handler.
    """
    invite_link = params.get('invite_link', '')
    name = params.get('name', '')

    ctx = FlowCtx.from_params(params)

    if not invite_link.startswith("quiet://invite/"):
        raise ValueError("Invalid invite link format")

    invite_b64 = invite_link[15:]
    # Support urlsafe + no padding, with fallback to standard base64
    try:
        padded = invite_b64 + '=' * (-len(invite_b64) % 4)
        invite_json = base64.urlsafe_b64decode(padded).decode()
        invite_data = json_module.loads(invite_json)
    except Exception:
        try:
            invite_json = base64.b64decode(invite_b64).decode()
            invite_data = json_module.loads(invite_json)
        except Exception:
            raise ValueError("Invalid invite link encoding")
    # Debug: surface invite keys to aid failure diagnosis
    try:
        keys = sorted(list(invite_data.keys())) if isinstance(invite_data, dict) else []
        print(f"[join_as_user] Decoded invite keys: {keys}")
    except Exception:
        pass

    invite_secret = invite_data.get('invite_secret')
    network_id = invite_data.get('network_id')
    group_id = invite_data.get('group_id')
    # Address owner KEM hints
    prekey_public = invite_data.get('prekey_public')
    prekey_secret_id_hint = invite_data.get('prekey_secret_id')
    # Transit DEM secret
    transit_secret_hex = invite_data.get('transit_secret')
    transit_secret_id = invite_data.get('transit_secret_id')
    # Join address
    join_ip = invite_data.get('ip') or '127.0.0.1'
    join_port = int(invite_data.get('port') or 8080)
    # Event-layer bootstrap key_secret
    invite_key_secret_hex = invite_data.get('invite_key_secret')
    invite_key_secret_id = None  # derive from secret
    print(f"[join_as_user] Invite data: network_id={network_id}, group_id={group_id}, invite_secret={invite_secret}")
    missing = [k for k, v in (('invite_secret', invite_secret), ('network_id', network_id), ('group_id', group_id)) if not v]
    if missing:
        raise ValueError(f"Invalid invite data - missing required fields: {missing}")

    if not name:
        raise ValueError("name is required for join_as_user")

    # ctx validates presence of db, runner, protocol_dir

    # 1) Local peer (peer_secret)
    priv, pub = generate_keypair()
    from core.crypto import get_root_secret_id
    root_id = get_root_secret_id()

    peer_secret_id = ctx.emit_event({
        'event_type': 'peer_secret',
        'event_plaintext': {
            'type': 'peer_secret',
            'name': name,
            'public_key': pub.hex(),
            'private_key': priv.hex(),
            'created_at': int(time.time() * 1000),
        },
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })
    public_key_hex = pub.hex()

    # 2) Persist invite-provided key_secret for event-layer bootstrap
    if not isinstance(invite_key_secret_hex, str):
        raise ValueError("Invalid invite key_secret in link")
    # Persist and capture the invite-provided key_secret id
    key_secret_id = ctx.emit_event({
        'event_type': 'key_secret',
        'event_plaintext': {
            'type': 'key_secret',
            'unsealed_secret': invite_key_secret_hex,
            'created_at': int(time.time() * 1000),
        },
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })

    # 2b) (moved below) Persist transit secret after peer is created so we can set owner ids

    # 3) Peer (encrypt using the invite key_secret we just persisted)
    peer_id = ctx.emit_event({
        'event_type': 'peer',
        'event_plaintext': {
            'type': 'peer',
            'public_key': public_key_hex,
            'peer_secret_id': peer_secret_id,
            'created_at': int(time.time() * 1000),
            'created_by': peer_secret_id,
        },
        'peer_id': peer_secret_id,
        'deps': [key_secret_id, peer_secret_id],
        'self_created': True,
        'key_secret_id': key_secret_id,
    })
    if not peer_id:
        raise ValueError(f"Failed to create peer for peer_secret {peer_secret_id}")

    # 3b) Persist transit secret locally in two forms now that we have peer_id
    try:
        if isinstance(transit_secret_hex, str) and isinstance(transit_secret_id, str) and transit_secret_hex and transit_secret_id:
            # Store as key_secret to enable event-layer encryption reuse if needed
            ctx.emit_event({
                'event_type': 'key_secret',
                'event_plaintext': {
                    'type': 'key_secret',
                    'unsealed_secret': transit_secret_hex,
                    'created_at': int(time.time() * 1000),
                },
                'peer_id': peer_id,
                'network_id': network_id,  # Helps local indexers attribute to network
                'local_only': True,
                'deps': [root_id],
                'self_created': True,
                'key_secret_id': root_id,
            })
            # Store as transit_secret carrying owner identity for attribution
            ctx.emit_event({
                'event_type': 'transit_secret',
                'event_plaintext': {
                    'type': 'transit_secret',
                    'unsealed_secret': transit_secret_hex,
                    'network_id': network_id,
                    'dest_ip': join_ip,
                    'dest_port': join_port,
                    'peer_secret_id': peer_secret_id,
                    'peer_id': peer_id,
                    'created_at': int(time.time() * 1000),
                },
                'local_only': True,
                'deps': [root_id],
                'self_created': True,
                'key_secret_id': root_id,
            })
    except Exception:
        pass

    # 4) User
    # Derive invite pubkey for proof from invite_secret
    invite_salt = hashlib.sha256(b"quiet_invite_kdf_v1").digest()[:16]
    derived_key = kdf(invite_secret.encode(), salt=invite_salt)
    invite_pubkey = derived_key.hex()
    invite_sig_data = f"{invite_secret}:{public_key_hex}:{network_id}"
    invite_signature = crypto_hash(invite_sig_data.encode()).hex()[:64]

    user_env = {
        'event_type': 'user',
        'event_plaintext': {
            'type': 'user',
            'peer_id': peer_id,
            'network_id': network_id,
            'group_id': group_id,
            'name': name,
            'invite_pubkey': invite_pubkey,
            'invite_signature': invite_signature,
            'created_at': int(time.time() * 1000),
            'created_by': peer_id,
        },
        'peer_id': peer_id,
        'peer_secret_id': peer_secret_id,  # Add hint for signature handler
        'deps': [peer_secret_id, network_id, group_id, key_secret_id],  # Use peer_secret_id for signing
        'self_created': True,
        # Create as normal event, not outgoing - let a job handle broadcasting later
        'key_secret_id': key_secret_id,
    }
    print(f"[user.join_as_user] About to emit user event with peer_id={peer_id}, peer_secret_id={peer_secret_id}")
    user_id = ctx.emit_event(user_env)
    print(f"[user.join_as_user] User event returned: user_id={user_id}")
    if not user_id:
        raise ValueError("Failed to create user after peer creation")

    # 5) Publish a prekey for this user and persist its local private part
    # Generate Ed25519 prekey pair (public used for SealedBox via conversion)
    pre_priv, pre_pub = generate_keypair()
    now_ms = int(time.time() * 1000)
    # Local-only prekey_secret carries private/public for unsealing
    prekey_secret_id = ctx.emit_event({
        'event_type': 'prekey_secret',
        'event_plaintext': {
            'type': 'prekey_secret',
            'prekey_private': pre_priv.hex(),
            'prekey_public': pre_pub.hex(),
            'network_id': network_id,
            'created_at': now_ms,
            'created_by': peer_id,
            'peer_id': peer_id,
            'peer_secret_id': peer_secret_id,
        },
        'peer_id': peer_id,
        'local_only': True,
        'self_created': True,
        'key_secret_id': root_id,
        'deps': [root_id],
    })
    # Public prekey event (projected into prekeys table)
    ctx.emit_event({
        'event_type': 'prekey',
        'event_plaintext': {
            'type': 'prekey',
            'prekey_id': prekey_secret_id,
            'peer_id': peer_id,
            'network_id': network_id,
            'public_key': pre_pub.hex(),
            'created_at': now_ms,
            'created_by': peer_id,
        },
        'peer_id': peer_id,
        'deps': [peer_id, peer_secret_id],
        'self_created': True,
    })

    # 6) Announce a local address for this peer (sim-friendly)
    try:
        port_offset = int(peer_id[-2:], 16) % 100
        demo_port = 6100 + port_offset
    except Exception:
        demo_port = 6100
    ctx.emit_event({
        'event_type': 'address',
        'event_plaintext': {
            'type': 'address',
            'action': 'add',
            'peer_id': peer_id,
            'ip': '127.0.0.1',
            'port': demo_port,
            'network_id': network_id,
            'timestamp_ms': int(time.time() * 1000),
            'created_by': peer_id,
        },
        'peer_id': peer_id,
        'deps': [peer_id, key_secret_id],
        'self_created': True,
        'is_outgoing': True,
        'key_secret_id': key_secret_id,
        'transit_secret_id': transit_secret_id,
        'transit_secret': bytes.fromhex(transit_secret_hex) if isinstance(transit_secret_hex, str) else transit_secret_hex,
        'dest_ip': join_ip,
        'dest_port': join_port,
    })

    # 7) Immediately send a sealed sync_request to inviter if prekey available in link
    # Send a stored, event-encrypted sync_request via transit sealing
    try:
        now_ms = int(time.time() * 1000)
        ctx.emit_event({
            'event_type': 'sync_request',
            'event_plaintext': {
                'type': 'sync_request',
                'request_id': hashlib.blake2b(f"{peer_id}:{now_ms}".encode(), digest_size=16).hexdigest(),
                'network_id': network_id,
                'created_by': peer_id,
                'timestamp_ms': now_ms,
                'sync_all': True,
                **({'prekey_public': prekey_public} if isinstance(prekey_public, str) and prekey_public else {}),
                **({'prekey_secret_id': prekey_secret_id_hint} if isinstance(prekey_secret_id_hint, str) and prekey_secret_id_hint else {}),
            },
            'peer_id': peer_id,
            'deps': [key_secret_id],
            'self_created': True,
            'is_outgoing': True,
            'key_secret_id': key_secret_id,
            'transit_secret_id': transit_secret_id,
            'transit_secret': bytes.fromhex(transit_secret_hex) if isinstance(transit_secret_hex, str) else transit_secret_hex,
            'dest_ip': join_ip,
            'dest_port': join_port,
        })
    except Exception:
        pass

    return {
        'ids': {
            'peer_secret': peer_secret_id,
            'peer': peer_id,
            'user': user_id,
        },
        'data': {
            'name': name,
            'joined': True,
        }
    }


@flow_op()  # Registers as 'user.create'
def create(params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a basic user event (without invite)."""
    ctx = FlowCtx.from_params(params)
    peer_id = params.get('peer_id', '')
    network_id = params.get('network_id', '')
    name = params.get('name', 'User')
    group_id = params.get('group_id', '')
    if not peer_id or not network_id:
        raise ValueError('peer_id and network_id are required')

    secret_id = params.get('secret_id', '')
    deps = [peer_id]
    if isinstance(secret_id, str) and secret_id:
        deps.append(secret_id)
    # Include signer peer_secret_id when available
    peer_secret_id = params.get('peer_secret_id')
    if isinstance(peer_secret_id, str) and peer_secret_id:
        deps.append(peer_secret_id)

    user_env = {
        'event_type': 'user',
        'event_plaintext': {
            'type': 'user',
            'peer_id': peer_id,
            'network_id': network_id,
            'group_id': group_id,
            'name': name,
            'created_at': int(time.time() * 1000),
            'created_by': peer_id,
        },
        'peer_id': peer_id,
        'peer_secret_id': peer_secret_id if peer_secret_id else None,  # Add hint for signature handler
        'deps': deps,
        'self_created': True,
    }
    user_id = ctx.emit_event(user_env)

    return {'ids': {'user': user_id}, 'data': {'user_id': user_id}}
