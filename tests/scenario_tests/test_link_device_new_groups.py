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
from db import Database
import schema
from events.identity import user, link_invite, link
from events.group import group, group_member
import tick


def test_link_device_sees_new_groups_after_invite():
    """Second device can see groups created between invite creation and device linking."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Alice creates network and Group A ===")

    # Alice creates network
    alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network")
    print(f"  user_id={alice_device1['user_id'][:20]}...")
    db.commit()

    # Create Group A and add Alice as member
    group_a_id, group_a_key_id = group.create(
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
    db.commit()

    # Sync for group creation
    print("\n=== Sync for Group A creation ===")
    for i in range(5):
        tick.tick(t_ms=2500 + i*100, db=db)

    # Alice creates link invite
    print("\n=== Alice creates link invite ===")

    invite_id, invite_link, invite_data = link_invite.create(
        peer_id=alice_device1['peer_id'],
        t_ms=3000,
        db=db
    )
    print(f"Link invite created: {invite_id[:20]}...")
    db.commit()

    # Alice creates Group B AFTER invite creation and adds herself as member
    print("\n=== Alice creates Group B (after invite) ===")

    group_b_id, group_b_key_id = group.create(
        name='Group B',
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=3500,
        db=db
    )
    print(f"Created Group B: {group_b_id[:20]}...")
    group_member.create(
        group_id=group_b_id,
        user_id=alice_device1['user_id'],
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=3501,
        db=db
    )
    db.commit()

    # Sync for Group B creation
    print("\n=== Sync for Group B creation ===")
    for i in range(5):
        tick.tick(t_ms=4000 + i*100, db=db)

    # Alice links second device
    print("\n=== Alice links second device ===")

    alice_device2 = link.join(
        link_url=invite_link,
        t_ms=5000,
        db=db
    )
    print(f"Alice linked device 2")
    print(f"  peer_id={alice_device2['peer_id'][:20]}...")
    print(f"  user_id={alice_device2['user_id'][:20]}...")
    db.commit()

    # Verify both devices have same user_id
    assert alice_device2['user_id'] == alice_device1['user_id']
    print(f"✅ Both devices share user_id")

    # Sync multiple rounds for convergence (more rounds needed for key sharing)
    print("\n=== Sync for link and group key propagation ===")
    for i in range(30):
        print(f"Sync round {i+1}...")
        tick.tick(t_ms=6000 + i*200, db=db)

    # Verify device 2 is member of BOTH groups
    print("\n=== Verifying device 2 group memberships ===")

    is_member_a = group_member.is_member(
        alice_device1['user_id'],
        group_a_id,
        alice_device2['peer_id'],
        db
    )
    print(f"Device 2 is member of Group A: {is_member_a}")
    assert is_member_a, "Device 2 should be member of Group A (existed before invite)"

    is_member_b = group_member.is_member(
        alice_device1['user_id'],
        group_b_id,
        alice_device2['peer_id'],
        db
    )
    print(f"Device 2 is member of Group B: {is_member_b}")
    assert is_member_b, "Device 2 should be member of Group B (created after invite)"

    print("✅ Device 2 is member of both groups")

    # Verify device 2 has keys for BOTH groups
    print("\n=== Verifying device 2 has group keys ===")

    has_key_a = db.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (group_a_key_id, alice_device2['peer_id'])
    )
    print(f"Device 2 has key for Group A: {bool(has_key_a)}")
    assert has_key_a, "Device 2 should have key for Group A"

    has_key_b = db.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (group_b_key_id, alice_device2['peer_id'])
    )
    print(f"Device 2 has key for Group B: {bool(has_key_b)}")
    assert has_key_b, "Device 2 should have key for Group B (even though created after invite)"

    print("✅ Device 2 has keys for both groups")

    print(f"\n✅ All assertions passed!")
