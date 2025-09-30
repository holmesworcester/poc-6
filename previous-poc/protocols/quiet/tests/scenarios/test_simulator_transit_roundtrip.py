"""
Scenario: Transit roundtrip via simulator using only API calls and one DB.

Alice and Bob in the same network; Alice posts a message; Bob syncs; network
simulator delivers packets; Bob ingests Alice's message. This covers transit
encryption path (sealed request + DEM responses) without manual frame building.
"""
from pathlib import Path
import time

from core.api import APIClient as API
from core import network as net


def test_transit_roundtrip_with_simulator_api(tmp_path):
    net.init_simulator()
    api = API(protocol_dir=Path('protocols/quiet'), reset_db=True, db_path=tmp_path / 'transit_api.db')

    # Alice bootstraps a network
    a_boot = api.execute_operation('network.create_as_user', {
        'username': 'Alice',
        'network_name': 'TransitNet',
    })
    a_id = a_boot['ids']['peer_secret']
    a_peer = a_boot['ids']['peer']
    net_id = a_boot['ids']['network']
    chan_id = a_boot['ids']['channel']
    # Address for Alice
    api.execute_operation('address.announce', {'peer_id': a_peer, 'ip': '127.0.0.1', 'port': 6401, 'network_id': net_id})

    # Bob joins via invite
    inv = api.execute_operation('invite.create', {'peer_id': a_peer, 'network_id': net_id, 'group_id': a_boot['ids']['group']})
    b_join = api.execute_operation('user.join_as_user', {'invite_link': inv['data']['invite_link'], 'name': 'Bob'})
    b_id = b_join['ids']['peer_secret']
    b_peer = b_join['ids']['peer']
    api.execute_operation('address.announce', {'peer_id': b_peer, 'ip': '127.0.0.1', 'port': 6402, 'network_id': net_id})

    # Alice posts a message
    api.execute_operation('message.create', {'peer_id': a_peer, 'channel_id': chan_id, 'content': 'Transit hello'})

    # Trigger sync; drive simulator; ingest
    api.execute_operation('sync_request.run', {})
    for _ in range(8):
        now = int(time.time() * 1000)
        api.execute_operation('network.tick', {'now_ms': now})

    # Bob should see Alice's message
    bob_view = api.execute_operation('message.get', {'peer_secret_id': b_id, 'channel_id': chan_id})
    assert any(m['content'] == 'Transit hello' for m in bob_view)
