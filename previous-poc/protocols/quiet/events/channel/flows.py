"""
Flows for channel operations.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.flows import FlowCtx, flow_op
import time


@flow_op()  # Registers as 'channel.create'
def create(params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = FlowCtx.from_params(params)
    group_id = params.get('group_id', '')

    peer_id = params.get('peer_id', '')
    name = params.get('name', '')
    network_id = params.get('network_id', '')
    if not peer_id:
        raise ValueError('peer_id is required for create_channel')
    if not group_id:
        raise ValueError('group_id is required for create_channel')

    key_secret_id = params.get('key_secret_id', '')
    deps = [f'group:{group_id}']
    if isinstance(key_secret_id, str) and key_secret_id:
        deps.append(key_secret_id)
    # Include signer peer_secret_id when available via normalize_actor
    peer_secret_id = params.get('peer_secret_id')
    if isinstance(peer_secret_id, str) and peer_secret_id:
        deps.append(peer_secret_id)

    env = {
        'event_type': 'channel',
        'event_plaintext': {
            'type': 'channel',
            'group_id': group_id,
            'name': name,
            'network_id': network_id,
            'creator_id': peer_id,
            'created_at': int(time.time() * 1000),
            'created_by': peer_id,
        },
        'peer_id': peer_id,
        'deps': deps,
        'self_created': True,
        'key_secret_id': key_secret_id,
    }
    new_channel_id = ctx.emit_event(env)

    cursor = ctx.db.execute(
        """
        SELECT channel_id, name, group_id, created_at
        FROM channels
        WHERE group_id = ?
        ORDER BY created_at DESC
        """,
        (group_id,),
    )

    channels: List[Dict[str, Any]] = []
    for row in cursor:
        channels.append({
            'channel_id': row[0],
            'name': row[1],
            'group_id': row[2],
            'created_at': row[3],
        })

    return {
        'ids': {'channel': new_channel_id},
        'data': {
            'channel_id': new_channel_id,
            'name': params.get('name', ''),
            'group_id': group_id,
            'channels': channels,
        },
    }
