"""
Scenario: Multi-client messaging over the network simulator using API only.

Two clients (separate DBs) join the same network via an invite. Each announces
an address. Client A posts a message. Client B issues a sync_request. Packets
move via the simulator (sealed KEM request and symmetric DEM responses). Client B
ingests and stores the event. We assert B has the message event id from A.
"""
from __future__ import annotations

import time
from pathlib import Path

from core.api import APIClient as API
from core import network as net


def test_multi_identity_chat_via_simulator_single_api(tmp_path):
    # Initialize simulator
    net.init_simulator()

    # Single API instance (single DB), multiple identities
    quiet_proto = Path(__file__).parent.parent.parent
    api = API(protocol_dir=quiet_proto, reset_db=True, db_path=tmp_path / 'chat.db')

    # Bootstrap Alice with a single API call
    a_boot = api.execute_operation('network.create_as_user', {
        'username': 'Alice',
        'network_name': 'ChatNet',
        'group_name': 'Main',
        'channel_name': 'general',
    })
    print(f"[test] a_boot result: {a_boot}")
    a_peer_secret = a_boot['ids']['peer_secret']  # Changed from identity
    a_peer = a_boot['ids']['peer']
    net_id = a_boot['ids']['network']
    group_id = a_boot['ids']['group']
    chan_id = a_boot['ids']['channel']
    key_secret_id = a_boot['data']['key_secret_id']  # Get key for encryption
    print(f"[test] Extracted IDs: peer={a_peer}, network={net_id}, group={group_id}, key_secret={key_secret_id}")

    # Note: address is already announced in create_as_user, no need to announce again

    # Create an invite for B
    invite = api.execute_operation('invite.create', {
        'peer_id': a_peer,
        'peer_secret_id': a_peer_secret,  # Added required param
        'network_id': net_id,
        'group_id': group_id,
    })
    invite_link = invite['data']['invite_link']

    # B joins via invite (creates identity, peer, user)
    b_join = api.execute_operation('user.join_as_user', {
        'invite_link': invite_link,
        'name': 'Bob',
    })
    b_peer_secret = b_join['ids']['peer_secret']  # Changed from identity
    b_peer = b_join['ids']['peer']

    # Note: address is already announced in join_as_user, no need to announce again

    # A posts a message (stored locally; later replicated via sync)
    m1 = api.execute_operation('message.create', {
        'peer_id': a_peer,
        'peer_secret_id': a_peer_secret,  # Added required param
        'key_secret_id': key_secret_id,  # Added for encryption
        'channel_id': chan_id,
        'content': 'Hello from Alice!'
    })
    print(f"[test] m1 result: {m1}")
    assert 'ids' in m1, f"message.create did not return ids: {m1}"
    assert 'message' in m1['ids'], f"message.create did not return message id: {m1}"
    m1_id = m1['ids']['message']
    assert m1_id, "Alice's message ID is empty"

    # Bob posts a message too
    m2 = api.execute_operation('message.create', {
        'peer_id': b_peer,
        'peer_secret_id': b_peer_secret,  # Added required param
        'key_secret_id': key_secret_id,  # Added for encryption (same network key)
        'channel_id': chan_id,
        'content': 'Hello from Bob!'
    })
    assert 'ids' in m2, f"message.create did not return ids: {m2}"
    assert 'message' in m2['ids'], f"message.create did not return message id: {m2}"
    m2_id = m2['ids']['message']
    assert m2_id, "Bob's message ID is empty"

    # Trigger sync requests across identities (both directions)
    api.execute_operation('sync_request.run', {
        'peer_id': a_peer,
        'peer_secret_id': a_peer_secret  # Added required param
    })

    # Drive network until idle: send_due and deliver_due and client ticks
    # A few iterations to cover sealed request -> reflect -> DEM responses
    for _ in range(12):
        now = int(time.time() * 1000)

        # Trigger the user job on each tick to broadcast joining events
        api.execute_operation('job.run', {
            'job_name': 'user',
        })

        # Advance simulator and ingest packets addressed to our local endpoints
        api.execute_operation('network.tick', {'now_ms': now})

    # Assert at the application level both identities can read each other's messages
    # (via message.get which enforces identity context)
    alice_msgs = api.execute_operation('message.get', {
        'peer_id': a_peer,
        'peer_secret_id': a_peer_secret,  # Use peer_secret_id
        'channel_id': chan_id,
    })
    bob_msgs = api.execute_operation('message.get', {
        'peer_id': b_peer,
        'peer_secret_id': b_peer_secret,  # Use peer_secret_id
        'channel_id': chan_id,
    })

    # Debug: Check what messages each user sees
    alice_contents = [m.get('content') for m in alice_msgs]
    bob_contents = [m.get('content') for m in bob_msgs]

    print(f"[test] Alice sees {len(alice_msgs)} messages: {alice_contents}")
    print(f"[test] Bob sees {len(bob_msgs)} messages: {bob_contents}")

    # Debug: Check the actual messages table
    import sqlite3
    db_path = api.db_path
    with sqlite3.connect(db_path) as db:
        cursor = db.cursor()
        all_messages = cursor.execute("SELECT message_id, author_id, content, network_id FROM messages ORDER BY created_at").fetchall()
        print(f"[test] All messages in DB: {all_messages}")

        # Check peers table
        all_peers = cursor.execute("SELECT peer_id, peer_secret_id FROM peers ORDER BY peer_id").fetchall()
        print(f"[test] All peers in DB: {all_peers}")

        # Check users table
        all_users = cursor.execute("SELECT user_id, peer_id, network_id, name FROM users ORDER BY user_id").fetchall()
        print(f"[test] All users in DB: {all_users}")

        # Test the message.get query logic manually for Alice
        alice_query = """
            SELECT m.*, u_author.name AS author_name
            FROM messages m
            JOIN users u_author ON u_author.peer_id = m.author_id
            WHERE EXISTS (
                SELECT 1
                FROM users u_me
                JOIN peers p_me ON p_me.peer_id = u_me.peer_id
                WHERE p_me.peer_secret_id = ?
                  AND u_me.network_id = m.network_id
            )
            AND m.channel_id = ?
            ORDER BY created_at DESC
        """
        alice_result = cursor.execute(alice_query, (a_peer_secret, chan_id)).fetchall()
        print(f"[test] Alice manual query result: {[r[5] for r in alice_result]}")  # content column

        # Test for Bob too
        bob_result = cursor.execute(alice_query, (b_peer_secret, chan_id)).fetchall()
        print(f"[test] Bob manual query result: {[r[5] for r in bob_result]}")  # content column

        # Check seen_events table to understand the "seen" filtering
        seen_events = cursor.execute("SELECT identity_id, event_id FROM seen_events ORDER BY identity_id, event_id").fetchall()
        print(f"[test] Seen events: {seen_events}")

        # Get message IDs for comparison
        msg_ids = [r[0] for r in all_messages]
        print(f"[test] Message IDs: {msg_ids}")
        print(f"[test] Alice peer_secret: {a_peer_secret}")
        print(f"[test] Bob peer_secret: {b_peer_secret}")

        # Check which messages each identity has seen
        alice_seen_messages = [e[1] for e in seen_events if e[0] == a_peer_secret and e[1] in msg_ids]
        bob_seen_messages = [e[1] for e in seen_events if e[0] == b_peer_secret and e[1] in msg_ids]
        print(f"[test] Alice has seen messages: {alice_seen_messages}")
        print(f"[test] Bob has seen messages: {bob_seen_messages}")

        # Pipeline stage analysis - let's trace where events are in the pipeline
        print(f"\n[test] === PIPELINE STAGE ANALYSIS ===")

        # Stage 1: Check what events exist and their visibility
        all_events = cursor.execute("""
            SELECT event_id, visibility
            FROM events
            ORDER BY event_id
        """).fetchall()
        network_events = [e for e in all_events if e[1] == 'network']
        local_events = [e for e in all_events if e[1] == 'local-only']
        print(f"[test] Events stored: {len(all_events)} total ({len(network_events)} network, {len(local_events)} local-only)")

        # Check if our messages are stored with network visibility
        message_events = [(e[0], e[1]) for e in all_events if e[0] in msg_ids]
        print(f"[test] Message events visibility: {message_events}")

        # Stage 2: Check network simulator state (basic info)
        print(f"[test] Network simulator:")
        try:
            print(f"  - Network simulator is active: {net.has_simulator()}")
            if net.has_simulator():
                print(f"  - Network ticks have been processed")
        except Exception as e:
            print(f"  - Network simulator info unavailable: {e}")

        # Stage 3: Check for events that should have been received but aren't seen
        print(f"[test] Cross-identity visibility:")
        for msg_id, author_id, content, network_id in all_messages:
            print(f"  Message '{content}' by {author_id[:8]}...")
            alice_has_seen = msg_id in alice_seen_messages
            bob_has_seen = msg_id in bob_seen_messages
            print(f"    Alice seen: {alice_has_seen}, Bob seen: {bob_has_seen}")

            if author_id.startswith(a_peer[:8]):  # Alice's message
                if not bob_has_seen:
                    print(f"    ❌ Bob should have seen Alice's message but didn't")
            elif author_id.startswith(b_peer[:8]):  # Bob's message
                if not alice_has_seen:
                    print(f"    ❌ Alice should have seen Bob's message but didn't")

        # Stage 4: Check key_secret events and network gate indexing
        print(f"[test] Network gate key_secret indexing:")
        try:
            # Check what key_secret events exist
            key_secrets = cursor.execute("""
                SELECT event_id, event_blob, visibility
                FROM events
                WHERE event_id LIKE '%key_secret%' OR event_blob LIKE '%key_secret%'
                ORDER BY event_id
            """).fetchall()
            print(f"  Key secret events by pattern: {len(key_secrets)}")

            # Check projected_events for key_secret type
            projected_keys = cursor.execute("""
                SELECT event_id, projection_data
                FROM projected_events
                WHERE event_type = 'key_secret'
                ORDER BY projected_at DESC
            """).fetchall()
            print(f"  Projected key_secret events: {len(projected_keys)}")
            for pk in projected_keys[:3]:
                import json
                try:
                    data = json.loads(pk[1])
                    key_secret_id = data.get('key_secret_id', 'N/A')
                    network_id = data.get('network_id', 'N/A')
                    print(f"    Event {pk[0][:8]}…: key_secret_id={key_secret_id[:8] if key_secret_id != 'N/A' else 'N/A'}…, network_id={network_id[:8] if network_id != 'N/A' else 'N/A'}…")
                except:
                    print(f"    Event {pk[0][:8]}…: (parse error)")

            # Check network_gate_transit_index
            transit_index = cursor.execute("""
                SELECT transit_secret_id, peer_id, network_id, created_at
                FROM network_gate_transit_index
                ORDER BY created_at DESC
            """).fetchall()
            print(f"  Network gate transit index entries: {len(transit_index)}")
            for ti in transit_index[:3]:
                print(f"    {ti[0][:8]}… -> peer:{ti[1][:8]}…, network:{ti[2][:8]}…")

        except Exception as e:
            print(f"  Network gate indexing check failed: {e}")

        # Stage 5: Check receive_from_network processing logs
        print(f"[test] Checking for receive_from_network attribution:")
        # This should show up in our pipeline logs when we re-run the sync
        print(f"  (Re-running sync to capture network processing...)")

        # Before re-sync, clear any existing to_peer logs we might see
        # Re-trigger sync to see network processing

    # First check if users can see their own messages
    assert 'Hello from Alice!' in alice_contents, f"Alice cannot see her own message. Alice sees: {alice_contents}"
    assert 'Hello from Bob!' in bob_contents, f"Bob cannot see his own message. Bob sees: {bob_contents}"

    # Then check cross-identity sync
    assert 'Hello from Bob!' in alice_contents, f"Alice did not see Bob's message. Alice sees: {alice_contents}"
    assert 'Hello from Alice!' in bob_contents, f"Bob did not see Alice's message. Bob sees: {bob_contents}"
