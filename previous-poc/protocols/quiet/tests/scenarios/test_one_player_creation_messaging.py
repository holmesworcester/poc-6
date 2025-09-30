"""
Scenario: Single-user bootstrap and local messaging via API only.

Follows scenarios/RULES.md:
- API-only (no direct DB access)
- Single DB, single API instance
- Identity isolation: specify acting identity on each call
"""
from __future__ import annotations

from pathlib import Path

from core.api import APIClient as API
from core import network as net


def test_one_player_creation_messaging(tmp_path):
    # Initialize simulator (harmless for single-user flows)
    net.init_simulator()

    # Single API instance (single DB)
    quiet_proto = Path('protocols/quiet')
    api = API(protocol_dir=quiet_proto, reset_db=True, db_path=tmp_path / 'one_player.db')

    # Bootstrap: create local peer + network + group + user + channel
    boot = api.execute_operation('network.create_as_user', {
        'username': 'Solo',
        'network_name': 'SoloNet',
        'group_name': 'Main',
        'channel_name': 'general',
    })
    ids = boot['ids']
    peer_secret = ids['peer_secret']
    peer = ids['peer']
    network = ids['network']
    group = ids['group']
    user = ids['user']
    channel = ids['channel']

    # Post a message as the created user
    m_res = api.execute_operation('message.create', {
        'peer_id': peer,
        'peer_secret_id': peer_secret,  # used for readback query in flow
        'channel_id': channel,
        'content': 'Hello, Solo!',
        # Use the bootstrap key for event-layer encryption until more keys exist
        'key_secret_id': boot['data']['key_secret_id'],
    })
    assert 'message' in m_res['ids'] and len(m_res['ids']['message']) == 32
    # Flow returns recent messages; verify our content is present
    msgs = m_res['data'].get('messages', [])
    assert any(m.get('content') == 'Hello, Solo!' for m in msgs), 'Posted message not visible to author'

    # Query users in the network from this identity's perspective
    users = api.execute_operation('user.get', {
        'peer_secret_id': peer_secret,
        'network_id': network,
    })
    # Expect to see ourselves as a member
    assert any(u.get('peer_id') == peer and (u.get('name') == 'Solo' or u.get('name') == 'solo') for u in users), \
        'User membership not visible to creator'

    # Direct message query to ensure consistent visibility
    msgs2 = api.execute_operation('message.get', {
        'peer_secret_id': peer_secret,
        'channel_id': channel,
    })
    assert any(m.get('content') == 'Hello, Solo!' for m in msgs2), 'Message not returned by message.get'
