"""
Flows for network operations.
"""
from __future__ import annotations

from typing import Dict, Any
import time

from core.flows import FlowCtx, flow_op
from core import crypto
from core import network as net


@flow_op()  # Registers as 'network.create'
def create(params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = FlowCtx.from_params(params)

    name = params.get('name', '')
    peer_id = params.get('peer_id')
    peer_secret_id = params.get('peer_secret_id')  # Need this for signing

    if not name:
        raise ValueError('name is required')
    if not peer_id:
        raise ValueError('peer_id is required - create a peer first')
    if not peer_secret_id:
        raise ValueError('peer_secret_id is required for signing')

    # Generate a new group secret for this network
    import nacl.utils as _utils
    net_secret = _utils.random(32)
    # Persist locally (key_secret) and capture its actual id
    from core.crypto import get_root_secret_id
    root_id = get_root_secret_id()
    key_secret_id = ctx.emit_event({
        'event_type': 'key_secret',
        'event_plaintext': {
            'type': 'key_secret',
            'unsealed_secret': net_secret.hex(),
            'created_at': int(time.time() * 1000),
        },
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })

    deps = [peer_secret_id, key_secret_id]  # Use peer_secret_id for signing

    network_id = ctx.emit_event({
        'event_type': 'network',
        'event_plaintext': {
            'type': 'network',
            'name': name,
            'created_by': peer_id,
            'created_at': int(time.time() * 1000),
        },
        'peer_id': peer_id,
        'deps': deps,
        'self_created': True,
        'key_secret_id': key_secret_id,
    })
    return {'ids': {'network': network_id}, 'data': {'key_secret_id': key_secret_id}}


@flow_op()  # Registers as 'network.tick'
def tick(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Drive the simulator and ingest any packets destined to our local addresses.

    Behavior:
    - net.send_due(now_ms)
    - net.deliver_due(now_ms) -> packets
    - Filter packets whose dest_ip/port match any active address for our local peers
    - Feed matching packets into the pipeline

    Returns: { data: {delivered: N, ingested: M} }
    """
    ctx = FlowCtx.from_params(params)
    now_ms = int(params.get('now_ms') or 0) or int(time.time() * 1000)

    try:
        sent = 0
        if hasattr(net, 'send_due') and net.has_simulator():
            sent = net.send_due(now_ms)
        delivered = net.deliver_due(now_ms) if hasattr(net, 'deliver_due') and net.has_simulator() else []

        # Build set of local endpoints via address query
        endpoints: set[tuple[str, int]] = set()
        try:
            eps = ctx.query('address.list_active', {}) or []
            for e in eps:
                endpoints.add((e.get('ip'), int(e.get('port'))))
        except Exception:
            pass

        # Ingest only packets addressed to our endpoints
        to_ingest = [pkt for pkt in delivered if (pkt.get('dest_ip'), int(pkt.get('dest_port'))) in endpoints]
        if to_ingest:
            ctx.runner.run(protocol_dir=ctx.protocol_dir, input_envelopes=to_ingest, db=ctx.db)
        return {'ids': {}, 'data': {'sent': sent, 'delivered': len(delivered), 'ingested': len(to_ingest)}}
    except Exception as e:
        return {'ids': {}, 'data': {'error': str(e)}}


@flow_op()  # Registers as 'network.create_as_user'
def create_as_user(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a new local peer_secret + peer and bootstrap a fresh network.

    Params:
    - username: display name for the peer_secret/user (required)
    - network_name: name of the network (required)
    - group_name: optional, default 'Main'
    - channel_name: optional, default 'general'

    Returns: { ids: {peer_secret, peer, network, group, user, channel} }
    """
    ctx = FlowCtx.from_params(params)

    username = (params.get('username') or '').strip()
    network_name = (params.get('network_name') or '').strip()
    group_name = (params.get('group_name') or 'Main')
    channel_name = (params.get('channel_name') or 'general')

    if not username:
        raise ValueError('username is required')
    if not network_name:
        raise ValueError('network_name is required')

    # 1) Local peer (peer_secret)
    priv, pub = crypto.generate_keypair()
    import hashlib
    # Emit and capture the actual peer_secret id; include deterministic field inside plaintext
    from core.crypto import get_root_secret_id
    root_id = get_root_secret_id()

    peer_secret_id = ctx.emit_event({
        'event_type': 'peer_secret',
        'event_plaintext': {
            'type': 'peer_secret',
            'name': username,
            'public_key': pub.hex(),
            'private_key': priv.hex(),
            'created_at': int(time.time() * 1000),
        },
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })

    # 2) Network secret (fresh)
    import nacl.utils as _utils2
    net_secret = _utils2.random(32)
    key_secret_id = ctx.emit_event({
        'event_type': 'key_secret',
        'event_plaintext': {
            'type': 'key_secret',
            'unsealed_secret': net_secret.hex(),
            'created_at': int(time.time() * 1000),
        },
        'local_only': True,
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })

    # 3) Peer (self-attested), encrypted with network key
    peer_id = ctx.emit_event({
        'event_type': 'peer',
        'event_plaintext': {
            'type': 'peer',
            'public_key': pub.hex(),
            'peer_secret_id': peer_secret_id,
            'created_at': int(time.time() * 1000),
            'created_by': peer_secret_id,
        },
        'peer_id': peer_secret_id,
        'deps': [key_secret_id],
        'self_created': True,
        'key_secret_id': key_secret_id,
    })

    # 4) Network
    network_id = ctx.emit_event({
        'event_type': 'network',
        'event_plaintext': {
            'type': 'network',
            'name': network_name,
            'created_by': peer_id,
            'created_at': int(time.time() * 1000),
        },
        'peer_id': peer_id,
        'deps': [peer_secret_id, key_secret_id],  # Use peer_secret_id for signing
        'self_created': True,
        'key_secret_id': key_secret_id,
    })

    # 5) Group
    group_id = ctx.emit_event({
        'event_type': 'group',
        'event_plaintext': {
            'type': 'group',
            'name': group_name,
            'network_id': network_id,
            'created_by': peer_id,
            'created_at': int(time.time() * 1000),
        },
        'peer_id': peer_id,
        'deps': [peer_secret_id, network_id, key_secret_id],  # Use peer_secret_id for signing
        'self_created': True,
        'key_secret_id': key_secret_id,
    })

    # 6) User (membership)
    user_id = ctx.emit_event({
        'event_type': 'user',
        'event_plaintext': {
            'type': 'user',
            'peer_id': peer_id,
            'network_id': network_id,
            'group_id': group_id,
            'name': username,
            'created_at': int(time.time() * 1000),
            'created_by': peer_id,
        },
        'peer_id': peer_id,
        'peer_secret_id': peer_secret_id,  # Add hint for signature handler
        'deps': [peer_secret_id, network_id, group_id, key_secret_id],  # Use peer_secret_id for signing
        'self_created': True,
        'key_secret_id': key_secret_id,
    })

    # 7) Default channel
    print(f"[network.create_as_user] About to emit channel with group_id={group_id}")
    channel_id = ctx.emit_event({
        'event_type': 'channel',
        'event_plaintext': {
            'type': 'channel',
            'group_id': group_id,
            'name': channel_name,
            'network_id': network_id,
            'creator_id': peer_id,  # Changed from created_by to match validator
            'created_at': int(time.time() * 1000),
        },
        'peer_id': peer_id,
        'deps': [peer_secret_id, group_id, key_secret_id],  # Use peer_secret_id for signing
        'self_created': True,
        'key_secret_id': key_secret_id,
    })
    print(f"[network.create_as_user] Channel ID returned: {channel_id}")

    # 7) Publish a prekey for this user and persist its local private part
    pre_priv, pre_pub = crypto.generate_keypair()
    now_ms = int(time.time() * 1000)
    # Local-only prekey_secret
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
        'deps': [root_id],
        'self_created': True,
        'key_secret_id': root_id,
    })
    # Public prekey event
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
        'deps': [peer_secret_id],  # Use peer_secret_id for signing
        'self_created': True,
    })

    # 8) Announce a local address for this peer (sim-friendly)
    # Deterministic port in 6100-6199 for single-process demo
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
        'deps': [peer_secret_id],  # Use peer_secret_id for signing
        'self_created': True,
    })

    return {
        'ids': {
            'peer_secret': peer_secret_id,
            'peer': peer_id,
            'network': network_id,
            'group': group_id,
            'user': user_id,
            'channel': channel_id,
        },
        'data': {
            'username': username,
            'network_name': network_name,
            'group_name': group_name,
            'channel_name': channel_name,
            'key_secret_id': key_secret_id,
        },
    }
