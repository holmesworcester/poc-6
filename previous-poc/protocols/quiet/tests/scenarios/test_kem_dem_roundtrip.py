"""
Scenario: KEM/DEM roundtrip for sync_request via API-only flows.

Using a single database with multiple identities, we create a network,
seed a message from Receiver (B), trigger sync_request.run, drive the
network simulator via network.tick, and verify Sender (A) ingests B's
message. This exercises: sealed KEM request -> reflect -> DEM responses.

Follows tests/scenarios/RULES.md: API-only, single DB, multi-identity.
"""
from __future__ import annotations

import time
from pathlib import Path

from core.api import APIClient as API
from core import network as net


def test_kem_dem_roundtrip_api_only(tmp_path):
    # Initialize network simulator (test harness)
    net.init_simulator()

    # Single API instance (single DB), multiple identities
    quiet_proto = Path('protocols/quiet')
    api = API(protocol_dir=quiet_proto, reset_db=True, db_path=tmp_path / 'kem_dem.db')

    # Bootstrap A (identity -> peer -> network -> group -> channel)
    a_id = api.execute_operation('peer_secret.create', {'name': 'Alice'})['ids']['peer_secret']
    a_key = api.execute_operation('key_secret.create', {})['ids']['key_secret']
    # Create peer directly via peer_secret.create by passing key_secret_id
    a_peer_create = api.execute_operation('peer_secret.create', {
        'name': 'Alice',
        'key_secret_id': a_key,
    })
    a_peer = a_peer_create['ids']['peer']
    net_id = api.execute_operation('network.create', {
        'peer_id': a_peer,
        'name': 'SyncNet'
    })['ids']['network']
    group_id = api.execute_operation('group.create', {
        'peer_id': a_peer,
        'network_id': net_id,
        'name': 'Main'
    })['ids']['group']
    chan_id = api.execute_operation('channel.create', {
        'peer_id': a_peer,
        'network_id': net_id,
        'group_id': group_id,
        'name': 'general'
    })['ids']['channel']
    # Ensure Alice is a member of the network for message visibility queries
    api.execute_operation('user.create', {
        'peer_id': a_peer,
        'network_id': net_id,
        'group_id': group_id,
        'name': 'Alice'
    })

    # A announces its address (simulated UDP endpoint)
    api.execute_operation('address.announce', {
        'peer_id': a_peer,
        'ip': '127.0.0.1',
        'port': 6201,
        'network_id': net_id,
    })

    # Create an invite for B and have B join via API
    invite = api.execute_operation('invite.create', {
        'peer_id': a_peer,
        'network_id': net_id,
        'group_id': group_id,
    })
    invite_link = invite['data']['invite_link']
    b_join = api.execute_operation('user.join_as_user', {
        'invite_link': invite_link,
        'name': 'Bob',
    })
    b_id = b_join['ids']['peer_secret']
    b_peer = b_join['ids']['peer']

    # B announces its address
    api.execute_operation('address.announce', {
        'peer_id': b_peer,
        'ip': '127.0.0.1',
        'port': 6202,
        'network_id': net_id,
    })

    # Seed a message from B that A should later receive via sync
    api.execute_operation('message.create', {
        'peer_id': b_peer,
        'channel_id': chan_id,
        'content': 'Message from Bob (pre-sync)'
    })

    # Trigger sync requests across identities
    api.execute_operation('sync_request.run', {})

    # Drive the simulator and ingest packets to local endpoints
    for _ in range(12):
        now = int(time.time() * 1000)
        api.execute_operation('network.tick', {'now_ms': now})

    # Verify A can read Bob's message via API query (no direct DB access)
    alice_view = api.execute_operation('message.get', {
        'peer_secret_id': a_id,
        'channel_id': chan_id,
    })
    assert any(m['content'] == 'Message from Bob (pre-sync)' for m in alice_view), \
        'Alice did not ingest Bob\'s message via KEM/DEM sync roundtrip'
