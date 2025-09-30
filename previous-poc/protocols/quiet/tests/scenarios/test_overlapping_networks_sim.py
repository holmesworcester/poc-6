"""
Scenario: Overlapping networks, API-only, single DB.

Alice belongs to Net1 and Net2; Bob belongs only to Net1; Charlie belongs only
to Net2. After sending messages in both networks and performing sync, Alice sees
both; Bob sees only Net1; Charlie sees only Net2.
"""
from pathlib import Path
import time

from core.api import APIClient as API
from core import network as net


def test_overlapping_networks_api_only(tmp_path):
    # Initialize simulator
    net.init_simulator()

    api = API(protocol_dir=Path('protocols/quiet'), reset_db=True, db_path=tmp_path / 'overlap.db')

    # Alice: peer_secret + bootstrap key + peer
    a_id = api.execute_operation('peer_secret.create', {'name': 'Alice'})['ids']['peer_secret']
    a_key = api.execute_operation('key_secret.create', {})['ids']['key_secret']
    a_peer = api.execute_operation('peer_secret.create', {'name': 'Alice', 'key_secret_id': a_key})['ids']['peer']

    # Net1
    net1 = api.execute_operation('network.create', {'peer_id': a_peer, 'name': 'Net1'})['ids']['network']
    g1 = api.execute_operation('group.create', {'peer_id': a_peer, 'network_id': net1, 'name': 'G1'})['ids']['group']
    c1 = api.execute_operation('channel.create', {'peer_id': a_peer, 'network_id': net1, 'group_id': g1, 'name': 'chan1'})['ids']['channel']
    # Ensure Alice is a user/member of Net1
    api.execute_operation('user.create', {'peer_id': a_peer, 'network_id': net1, 'group_id': g1, 'name': 'Alice'})
    api.execute_operation('address.announce', {'peer_id': a_peer, 'ip': '127.0.0.1', 'port': 6301, 'network_id': net1})
    inv1 = api.execute_operation('invite.create', {'peer_id': a_peer, 'network_id': net1, 'group_id': g1})

    # Net2
    net2 = api.execute_operation('network.create', {'peer_id': a_peer, 'name': 'Net2'})['ids']['network']
    g2 = api.execute_operation('group.create', {'peer_id': a_peer, 'network_id': net2, 'name': 'G2'})['ids']['group']
    c2 = api.execute_operation('channel.create', {'peer_id': a_peer, 'network_id': net2, 'group_id': g2, 'name': 'chan2'})['ids']['channel']
    # Ensure Alice is a user/member of Net2
    api.execute_operation('user.create', {'peer_id': a_peer, 'network_id': net2, 'group_id': g2, 'name': 'Alice'})
    api.execute_operation('address.announce', {'peer_id': a_peer, 'ip': '127.0.0.1', 'port': 6301, 'network_id': net2})
    inv2 = api.execute_operation('invite.create', {'peer_id': a_peer, 'network_id': net2, 'group_id': g2})

    # Bob joins Net1 only
    b_join = api.execute_operation('user.join_as_user', {'invite_link': inv1['data']['invite_link'], 'name': 'Bob'})
    b_id = b_join['ids']['peer_secret']
    b_peer = b_join['ids']['peer']
    api.execute_operation('address.announce', {'peer_id': b_peer, 'ip': '127.0.0.1', 'port': 6302, 'network_id': net1})

    # Charlie joins Net2 only
    c_join = api.execute_operation('user.join_as_user', {'invite_link': inv2['data']['invite_link'], 'name': 'Charlie'})
    c_id = c_join['ids']['peer_secret']
    c_peer = c_join['ids']['peer']
    api.execute_operation('address.announce', {'peer_id': c_peer, 'ip': '127.0.0.1', 'port': 6303, 'network_id': net2})

    # Alice posts one message in each network
    api.execute_operation('message.create', {'peer_id': a_peer, 'channel_id': c1, 'content': 'Net1: hello from Alice'})
    api.execute_operation('message.create', {'peer_id': a_peer, 'channel_id': c2, 'content': 'Net2: hello from Alice'})

    # Run sync
    api.execute_operation('sync_request.run', {})
    for _ in range(12):
        now = int(time.time() * 1000)
        api.execute_operation('network.tick', {'now_ms': now})

    # Alice sees both
    a_msgs_1 = api.execute_operation('message.get', {'peer_secret_id': a_id, 'channel_id': c1})
    a_msgs_2 = api.execute_operation('message.get', {'peer_secret_id': a_id, 'channel_id': c2})
    assert any('Net1: hello from Alice' in m['content'] for m in a_msgs_1)
    assert any('Net2: hello from Alice' in m['content'] for m in a_msgs_2)

    # Bob sees only Net1
    b_msgs_1 = api.execute_operation('message.get', {'peer_secret_id': b_id, 'channel_id': c1})
    assert any('Net1: hello from Alice' in m['content'] for m in b_msgs_1)
    b_msgs_2 = api.execute_operation('message.get', {'peer_secret_id': b_id, 'channel_id': c2})
    assert not any('Net2:' in m['content'] for m in b_msgs_2)

    # Charlie sees only Net2
    c_msgs_1 = api.execute_operation('message.get', {'peer_secret_id': c_id, 'channel_id': c1})
    assert not any('Net1:' in m['content'] for m in c_msgs_1)
    c_msgs_2 = api.execute_operation('message.get', {'peer_secret_id': c_id, 'channel_id': c2})
    assert any('Net2: hello from Alice' in m['content'] for m in c_msgs_2)
