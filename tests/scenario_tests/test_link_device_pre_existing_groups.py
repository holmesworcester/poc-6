"""
Scenario test: Link device with pre-existing groups.

Alice creates a network and several groups, then links a second device.
The second device should be able to see all groups that existed before the link was created.

Tests:
- Alice creates network on first device
- Alice creates multiple groups (A, B, C)
- Alice creates link invite
- Alice links second device
- Second device can see all pre-existing groups

Note: In sender key model, there are no group_keys. Each sender has their own key.
We test that the linked device can see groups and memberships.
"""
import sqlite3
from core.db import Database, create_safe_db
from core import schema
from events.identity import user, invite, peer_shared, peer
from events.group import group, group_member
from tests.utils.tick_helper import assert_eventually


def test_link_device_sees_pre_existing_groups(fresh_db):
    """Second device can see groups created before link invite."""

    # Setup
    db = fresh_db

    print("\n=== Setup: Alice creates network on first device ===")

    # Alice creates network on first device
    alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network")
    print(f"  user_id={alice_device1['user_id'][:20]}...")
    db.commit()

    # Alice creates multiple groups
    print("\n=== Alice creates Groups A, B, C ===")

    group_a_id, _ = group.create(
        name='Group A',
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=2000,
        db=db
    )
    print(f"Created Group A: {group_a_id[:20]}...")
    group_member.create(
        group_id=group_a_id,
        user_id=alice_device1['user_id'],
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=2001,
        db=db
    )

    group_b_id, _ = group.create(
        name='Group B',
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=2100,
        db=db
    )
    print(f"Created Group B: {group_b_id[:20]}...")
    group_member.create(
        group_id=group_b_id,
        user_id=alice_device1['user_id'],
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=2101,
        db=db
    )

    group_c_id, _ = group.create(
        name='Group C',
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=2200,
        db=db
    )
    print(f"Created Group C: {group_c_id[:20]}...")
    group_member.create(
        group_id=group_c_id,
        user_id=alice_device1['user_id'],
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=2201,
        db=db
    )
    db.commit()

    # Alice creates peer invite for device linking
    print("\n=== Alice creates peer invite for device linking ===")

    invite_id, invite_link, _ = invite.create(
        peer_id=alice_device1['peer_id'],
        t_ms=3000,
        db=db,
        mode='peer',
        user_id=alice_device1['user_id']
    )
    print(f"Peer invite created: {invite_id[:20]}...")
    db.commit()

    # Alice links second device
    print("\n=== Alice links second device ===")

    alice_device2_peer_id = peer.create(t_ms=5000, db=db)
    accepted = invite.accept(alice_device2_peer_id, invite_link, t_ms=5001, db=db)
    assert accepted['mode'] == 'peer'
    alice_device2 = peer_shared.join(
        peer_id=alice_device2_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        t_ms=5002,
        db=db,
        network_id=accepted.get('network_id')
    )
    print(f"Alice linked device 2")
    print(f"  peer_id={alice_device2['peer_id'][:20]}...")
    print(f"  user_id={alice_device2['user_id'][:20]}...")
    db.commit()

    # Verify both devices have same user_id
    assert alice_device2['user_id'] == alice_device1['user_id'], \
        "Both devices should have same user_id"
    print(f"✅ Both devices share user_id: {alice_device1['user_id'][:20]}...")

    # Wait for device 2 to see all groups
    print("\n=== Waiting for device 2 to see all groups ===")

    def device2_sees_all_groups():
        safedb = create_safe_db(db, recorded_by=alice_device2['peer_id'])
        groups = safedb.query(
            "SELECT group_id, name FROM groups WHERE recorded_by = ?",
            (alice_device2['peer_id'],)
        )
        group_names = set(g['name'] for g in groups)
        # Should see Groups A, B, C (plus any default groups)
        assert 'Group A' in group_names, "Device 2 should see Group A"
        assert 'Group B' in group_names, "Device 2 should see Group B"
        assert 'Group C' in group_names, "Device 2 should see Group C"

    t_ms = assert_eventually(device2_sees_all_groups, db=db, start_t_ms=None)

    # Verify device 2 is a member of all groups
    print("\n=== Verifying device 2 is member of all groups ===")

    def device2_is_member_of_all():
        is_member_a = group_member.is_member(
            alice_device1['user_id'],
            group_a_id,
            alice_device2['peer_id'],
            db
        )
        is_member_b = group_member.is_member(
            alice_device1['user_id'],
            group_b_id,
            alice_device2['peer_id'],
            db
        )
        is_member_c = group_member.is_member(
            alice_device1['user_id'],
            group_c_id,
            alice_device2['peer_id'],
            db
        )
        assert is_member_a, "Device 2 should be member of Group A"
        assert is_member_b, "Device 2 should be member of Group B"
        assert is_member_c, "Device 2 should be member of Group C"

    assert_eventually(device2_is_member_of_all, db=db, start_t_ms=t_ms)
    print("✅ Device 2 can see and is member of all pre-existing groups")

    print(f"\n✅ All assertions passed!")
