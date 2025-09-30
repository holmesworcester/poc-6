"""
Flows for message operations.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.flows import FlowCtx, flow_op
import time


@flow_op()  # Registers as 'message.create'
def create(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Emit message.create_message and return recent messages in the channel.
    """
    ctx = FlowCtx.from_params(params)
    channel_id = params.get('channel_id', '')

    peer_id = params.get('peer_id', '')
    if not peer_id:
        raise ValueError('peer_id is required for create_message')

    key_secret_id = params.get('key_secret_id', '')
    # Dependencies are event IDs only; resolver extracts channel_id etc from plaintext
    deps = []
    if isinstance(key_secret_id, str) and key_secret_id:
        deps.append(key_secret_id)
    # Add channel_id as dependency so it's validated before the message
    if isinstance(channel_id, str) and channel_id:
        deps.append(channel_id)
    # Add peer_id as dependency so signature handler can find the signer
    if isinstance(peer_id, str) and peer_id:
        deps.append(peer_id)
    # Add peer_secret_id as dependency for direct access to signing key
    # (local-only events ARE in the events table, just encrypted with root secret)
    peer_secret_id = params.get('peer_secret_id') or params.get('peer_secret') or ''
    if isinstance(peer_secret_id, str) and peer_secret_id:
        deps.append(peer_secret_id)

    # Look up the channel to get network_id and group_id using query API
    network_id = ''
    group_id = ''
    if channel_id:
        try:
            channel_info = ctx.query('channel.get_by_id', {'channel_id': channel_id})
            if channel_info:
                network_id = channel_info.get('network_id', '')
                group_id = channel_info.get('group_id', '')
        except Exception:
            # Channel not found or query failed - will have empty network_id and group_id
            pass

    # If key_secret_id not provided, we can't easily look it up
    # The crypto handler will need to handle messages without explicit key_secret_id
    # by finding the key from other dependencies

    env = {
        'event_type': 'message',
        'event_plaintext': {
            'type': 'message',
            'channel_id': channel_id,
            'group_id': group_id,
            'network_id': network_id,
            'created_by': peer_id,
            'content': params.get('content', ''),
            'created_at': int(time.time() * 1000),
        },
        'peer_id': peer_id,
        'deps': deps,
        'self_created': True,
        'key_secret_id': key_secret_id,
    }

    # peer_secret_id is now in deps, so signature handler will get it via resolved_deps
    # We can still pass it as a hint for backwards compatibility
    if peer_secret_id:
        env['peer_secret_id'] = peer_secret_id
    new_message_id = ctx.emit_event(env)

    # Use query API for read
    # Note: peer_secret_id required by message.get; caller should pass it in params if available
    peer_secret_id = params.get('peer_secret_id') or params.get('peer_secret') or ''
    messages: List[Dict[str, Any]] = []
    try:
        if peer_secret_id:
            messages = ctx.query('message.get', {
                'peer_secret_id': peer_secret_id,
                'channel_id': channel_id,
                'limit': 50,
            })
    except Exception:
        messages = []

    return {
        'ids': {'message': new_message_id},
        'data': {
            'message_id': new_message_id,
            'channel_id': channel_id,
            'content': params.get('content', ''),
            'messages': messages,
        },
    }
