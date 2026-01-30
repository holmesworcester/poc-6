"""
Scenario test: Link device with groups added between invite creation and join.

Alice creates link invite, then joins a new group, then second device accepts the link.
The second device should be able to see groups added after the invite was created but before the device linked.

Tests:
- Alice creates network and Group A
- Alice creates link invite
- Alice creates Group B (after invite, before link acceptance)
- Alice links second device
- Second device can see BOTH Group A and Group B
- Second device has keys for both groups
"""
import sqlite3
from core.db import Database
from core import schema
from events.identity import user, invite, peer_shared, peer
from events.group import group, group_member
from tests.utils.tick_helper import assert_eventually, TestClock


def test_link_device_sees_new_groups_after_invite(fresh_db):
    """Second device can see groups created between invite creation and device linking."""

    # Setup
    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network and Group A ===")

    # Alice creates network
    alice_device1 = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    print(f"Alice created network")
    print(f"  user_id={alice_device1['user_id'][:20]}...")
    db.commit()

    # Create Group A and add Alice as member
    group_a_id, group_a_key_id = group.create(
        name='Group A',
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    print(f"Created Group A: {group_a_id[:20]}...")
    group_member.create(
        group_id=group_a_id,
        user_id=alice_device1['user_id'],
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Alice creates peer invite for device linking
    print("\n=== Alice creates peer invite for device linking ===")

    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice_device1['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='peer',
        user_id=alice_device1['user_id']
    )
    print(f"Peer invite created: {invite_id[:20]}...")
    db.commit()

    # Alice creates Group B AFTER invite creation and adds herself as member
    print("\n=== Alice creates Group B (after invite) ===")

    group_b_id, group_b_key_id = group.create(
        name='Group B',
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    print(f"Created Group B: {group_b_id[:20]}...")
    group_member.create(
        group_id=group_b_id,
        user_id=alice_device1['user_id'],
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Alice links second device via the link URL
    print("\n=== Alice links second device via peer invite ===")

    # Create peer for device 2
    alice_device2_peer_id = peer.create(t_ms=clock.tick(), db=db)

    # Accept the invite
    accepted = invite.accept(alice_device2_peer_id, invite_link, t_ms=clock.tick(), db=db)
    assert accepted['mode'] == 'peer'

    # Complete the peer linking
    alice_device2 = peer_shared.join(
        peer_id=alice_device2_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=clock.tick(),
        db=db
    )
    print(f"Alice linked device 2")
    print(f"  peer_id={alice_device2['peer_id'][:20]}...")
    print(f"  user_id={alice_device2['user_id'][:20]}...")
    db.commit()

    # Verify both devices have same user_id
    assert alice_device2['user_id'] == alice_device1['user_id']
    print(f"✅ Both devices share user_id")

    # Wait for device 2 to sync group keys
    print("\n=== Waiting for device 2 to sync group keys ===")

    def device2_has_both_keys():
        has_key_a = db.query_one(
            "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
            (group_a_key_id, alice_device2['peer_id'])
        )
        has_key_b = db.query_one(
            "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
            (group_b_key_id, alice_device2['peer_id'])
        )
        assert has_key_a, "Device 2 should have Group A key"
        assert has_key_b, "Device 2 should have Group B key"

    assert_eventually(device2_has_both_keys, db=db, start_t_ms=None)
    print("✅ Device 2 has both group keys")

    # Now check that device 2 can see both groups
    print("\n=== Verifying device 2 can see both groups ===")

    def device2_sees_groups():
        groups_d2 = db.query_all(
            "SELECT DISTINCT group_id, name FROM groups WHERE recorded_by = ?",
            (alice_device2['peer_id'],)
        )
        group_names = {g['name'] for g in groups_d2}
        assert 'Group A' in group_names, f"Device 2 should see Group A, got: {group_names}"
        assert 'Group B' in group_names, f"Device 2 should see Group B, got: {group_names}"

    assert_eventually(device2_sees_groups, db=db, start_t_ms=None)
    print("✅ Device 2 can see both Group A and Group B")

    print("\n=== All assertions passed! ===")
