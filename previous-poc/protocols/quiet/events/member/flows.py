"""
Flows for member operations.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from core.flows import FlowCtx, flow_op


@flow_op()  # Registers as 'member.create'
def create(params: Dict[str, Any]) -> Dict[str, Any]:
    ctx = FlowCtx.from_params(params)
    group_id = params.get('group_id', '')
    user_id = params.get('user_id', '')
    peer_id = params.get('peer_id') or params.get('identity_id', '')
    network_id = params.get('network_id', '')

    if not group_id or not user_id or not peer_id or not network_id:
        raise ValueError('group_id, user_id, peer_id, network_id are required')

    env = {
        'event_type': 'member',
        'event_plaintext': {
            'type': 'member',
            'group_id': group_id,
            'user_id': user_id,
            'added_by': peer_id,
            'network_id': network_id,
            'created_at': int(time.time() * 1000),
            'created_by': peer_id,
        },
        'peer_id': peer_id,
        'deps': [group_id, user_id] + ([params.get('peer_secret_id')] if isinstance(params.get('peer_secret_id'), str) and params.get('peer_secret_id') else []),
        'self_created': True,
    }
    add_id = ctx.emit_event(env)

    members: List[Dict[str, Any]] = ctx.query('member.list_by_group', {'group_id': group_id}) or []

    return {
        'ids': {'member': add_id},
        'data': {
            'group_id': group_id,
            'members': members,
            'member_count': len(members),
        },
    }
