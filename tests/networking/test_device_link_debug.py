"""Debug test for device linking over real UDP.

This test has extra logging to diagnose sync issues.

SKIPPED: These tests use the old queues.set_transport_callback() system
which is incompatible with the new core/transport.py system. Real networking
tests need to be refactored to use multi-instance (separate processes).
"""

import pytest

# Skip all tests in this module - old transport callback system incompatible with new transport.py
pytestmark = pytest.mark.skip(reason="Uses old transport callback system; needs multi-instance refactor")
from events.identity import user, invite, peer, peer_shared
from events.content import message
from tests.networking.conftest import (
    RealClient, setup_transport_callback, tick_until, now_ms, get_unique_ports
)
from core import queues
from core.db import create_safe_db


@pytest.fixture
def device_link_debug_clients(tmp_dir):
    """Create clients for device link debugging."""
    ports = get_unique_ports(2)

    alice_phone = RealClient("AlicePhone", f"{tmp_dir}/alice_phone.db", port=ports[0])
    alice_laptop = RealClient("AliceLaptop", f"{tmp_dir}/alice_laptop.db", port=ports[1])

    clients = {"alice_phone": alice_phone, "alice_laptop": alice_laptop}
    route_stats = setup_transport_callback(clients)

    yield clients, route_stats

    # Cleanup
    queues.set_transport_callback(None)
    alice_phone.stop()
    alice_laptop.stop()


def test_device_link_debug(device_link_debug_clients):
    """Debug device link sync flow."""
    clients, route_stats = device_link_debug_clients
    alice_phone = clients["alice_phone"]
    alice_laptop = clients["alice_laptop"]

    print("\n=== PHASE 1: Alice creates network on phone ===")
    t_ms = now_ms()
    result = user.new_network(name="DebugNet", t_ms=t_ms, db=alice_phone.db)
    alice_phone.peer_id = result['peer_id']
    alice_phone.peer_shared_id = result['peer_shared_id']
    alice_phone.user_id = result['user_id']
    alice_phone.network_id = result['network_id']
    alice_phone.channel_id = result['channel_id']
    alice_phone.db.commit()

    print(f"Phone peer_id: {alice_phone.peer_id[:30]}...")
    print(f"Phone peer_shared_id: {alice_phone.peer_shared_id[:30]}...")
    print(f"Phone user_id: {alice_phone.user_id[:30]}...")

    # Check phone's peer_self
    phone_safedb = create_safe_db(alice_phone.db, recorded_by=alice_phone.peer_id)
    phone_self = phone_safedb.query_one(
        "SELECT * FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (alice_phone.peer_id, alice_phone.peer_id)
    )
    print(f"Phone peer_self: {phone_self}")

    print("\n=== PHASE 2: Create device link invite ===")
    t_ms = now_ms()
    link_invite_id, link_code, link_data = invite.create(
        peer_id=alice_phone.peer_id,
        t_ms=t_ms,
        db=alice_phone.db,
        mode='peer',
        user_id=alice_phone.user_id
    )
    alice_phone.db.commit()

    print(f"Link invite_id: {link_invite_id[:30]}...")
    print(f"Link invite has inviter_peer_shared_id: {link_data.get('inviter_peer_shared_id', 'MISSING')[:30]}...")
    print(f"Link invite has transit_prekey: {'YES' if link_data.get('inviter_connection_prekey_public_key') else 'NO'}")

    print("\n=== PHASE 3: Laptop creates peer and accepts invite ===")
    t_ms = now_ms()
    alice_laptop.peer_id = peer.create(t_ms=t_ms, db=alice_laptop.db)
    alice_laptop.db.commit()
    print(f"Laptop peer_id: {alice_laptop.peer_id[:30]}...")

    t_ms = now_ms()
    accepted = invite.accept(alice_laptop.peer_id, link_code, t_ms=t_ms, db=alice_laptop.db)
    alice_laptop.db.commit()

    print(f"Accepted mode: {accepted['mode']}")
    print(f"Accepted inviter_peer_shared_id: {accepted.get('inviter_peer_shared_id', 'MISSING')[:30]}...")

    # Check laptop's invite_accepteds
    laptop_safedb = create_safe_db(alice_laptop.db, recorded_by=alice_laptop.peer_id)
    invite_accepted_row = laptop_safedb.query_one(
        "SELECT * FROM invite_accepteds WHERE recorded_by = ? LIMIT 1",
        (alice_laptop.peer_id,)
    )
    print(f"Laptop invite_accepteds row: invite_id={invite_accepted_row['invite_id'][:20]}..., inviter={invite_accepted_row.get('inviter_peer_shared_id', 'NULL')}")

    print("\n=== PHASE 4: Laptop joins via peer_shared.join ===")
    t_ms = now_ms()
    link_result = peer_shared.join(
        peer_id=alice_laptop.peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=t_ms,
        db=alice_laptop.db
    )
    alice_laptop.peer_shared_id = link_result['peer_shared_id']
    alice_laptop.user_id = link_result['user_id']
    alice_laptop.db.commit()

    print(f"Laptop peer_shared_id: {alice_laptop.peer_shared_id[:30]}...")
    print(f"Laptop user_id: {alice_laptop.user_id[:30]}...")

    # Verify same user_id
    assert alice_laptop.user_id == alice_phone.user_id, "Linked devices should share user_id"
    print("User IDs match!")

    # Check laptop's peer_self
    laptop_self = laptop_safedb.query_one(
        "SELECT * FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (alice_laptop.peer_id, alice_laptop.peer_id)
    )
    print(f"Laptop peer_self: peer_shared_id={laptop_self['peer_shared_id'][:20] if laptop_self else 'NULL'}...")

    print("\n=== PHASE 5: Ticking to sync ===")
    print(f"Route stats before ticking: {route_stats}")

    # Tick more times and check status
    for i in range(100):
        t_ms = now_ms()
        alice_phone.tick(t_ms)
        alice_laptop.tick(t_ms)

        if i % 20 == 0:
            # Check connections
            phone_conns = phone_safedb.query(
                "SELECT peer_shared_id, their_connection_id FROM connections WHERE recorded_by = ?",
                (alice_phone.peer_id,)
            )
            laptop_conns = laptop_safedb.query(
                "SELECT peer_shared_id, their_connection_id FROM connections WHERE recorded_by = ?",
                (alice_laptop.peer_id,)
            )
            print(f"Tick {i}: Phone connections={len(phone_conns)}, Laptop connections={len(laptop_conns)}")

            # Check if phone has laptop's peer_shared
            phone_has_laptop = phone_safedb.query_one(
                "SELECT 1 FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
                (alice_laptop.peer_shared_id, alice_phone.peer_id)
            )
            print(f"Tick {i}: Phone has laptop's peer_shared: {phone_has_laptop is not None}")

            # Check group_keys on laptop
            laptop_keys = laptop_safedb.query(
                "SELECT key_id FROM group_keys WHERE recorded_by = ?",
                (alice_laptop.peer_id,)
            )
            print(f"Tick {i}: Laptop has {len(laptop_keys)} group keys")

        import time
        time.sleep(0.05)

    print(f"\nRoute stats after ticking: {route_stats}")

    # Check final state
    print("\n=== FINAL STATE ===")

    # Phone's view of laptop
    phone_has_laptop = phone_safedb.query_one(
        "SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
        (alice_laptop.peer_shared_id, alice_phone.peer_id)
    )
    print(f"Phone has laptop's peer_shared: {phone_has_laptop is not None}")

    # Laptop's group keys
    laptop_keys = laptop_safedb.query(
        "SELECT key_id FROM group_keys WHERE recorded_by = ?",
        (alice_laptop.peer_id,)
    )
    print(f"Laptop group keys: {len(laptop_keys)}")

    # Laptop's channels
    from events.content import channel
    laptop_channels = channel.list(alice_laptop.peer_id, alice_laptop.db)
    print(f"Laptop channels: {len(laptop_channels)}")
    for ch in laptop_channels:
        print(f"  - {ch['channel_id'][:20]}... name={ch.get('name', 'unknown')}")

    phone_channels = channel.list(alice_phone.peer_id, alice_phone.db)
    print(f"Phone channels: {len(phone_channels)}")
    for ch in phone_channels:
        print(f"  - {ch['channel_id'][:20]}... name={ch.get('name', 'unknown')}")

    # The actual assertion
    has_channel = any(ch['channel_id'] == alice_phone.channel_id for ch in laptop_channels)
    print(f"\nLaptop has phone's channel: {has_channel}")

    if not has_channel:
        print("\n=== DEBUGGING MISSING CHANNEL ===")

        # Check group_keys_shared on phone
        phone_gks = phone_safedb.query(
            "SELECT * FROM group_keys_shared WHERE recorded_by = ?",
            (alice_phone.peer_id,)
        )
        print(f"Phone group_keys_shared entries: {len(phone_gks)}")

        # Check shareable events on phone
        phone_shareable = phone_safedb.query(
            "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? LIMIT 10",
            (alice_phone.peer_id,)
        )
        print(f"Phone shareable events (first 10): {len(phone_shareable)}")

        # Check what's in laptop's valid_events
        laptop_valid = laptop_safedb.query(
            "SELECT event_id FROM valid_events WHERE recorded_by = ? LIMIT 20",
            (alice_laptop.peer_id,)
        )
        print(f"Laptop valid events (first 20): {len(laptop_valid)}")

    # For now, just print results (actual test would assert)
    print("\n=== TEST COMPLETE ===")
