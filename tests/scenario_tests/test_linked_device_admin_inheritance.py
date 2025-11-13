"""
Scenario test: Linked devices inherit admin privileges.

Alice creates a network (becomes admin via first_peer), then links a second device.
The second device should also be admin and be able to create invites.

Tests:
- Alice creates network (becomes admin via first_peer)
- Alice links second device
- Second device is also admin
- Second device can create invites for new users
- New user can successfully join via second device's invite
"""
import sqlite3
from db import Database
import schema
from events.identity import user, link_invite, link, invite
from events.group import group_member
import tick


def test_linked_device_inherits_admin_privileges():
    """Linked device inherits admin privileges from user."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Alice creates network (becomes admin) ===")

    # Alice creates network
    alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network on device 1")
    print(f"  user_id={alice_device1['user_id'][:20]}...")
    print(f"  peer_shared_id={alice_device1['peer_shared_id'][:20]}...")
    db.commit()

    # Verify Alice is admin on device 1
    is_admin_device1 = invite.is_admin(
        alice_device1['peer_shared_id'],
        alice_device1['peer_id'],
        db
    )
    print(f"Device 1 is admin: {is_admin_device1}")
    assert is_admin_device1, "Device 1 should be admin (network creator)"
    print("✅ Device 1 is admin")

    # Alice links second device
    print("\n=== Alice links second device ===")

    invite_id, invite_link, _ = link_invite.create(
        peer_id=alice_device1['peer_id'],
        t_ms=2000,
        db=db
    )
    db.commit()

    alice_device2 = link.join(
        link_url=invite_link,
        t_ms=3000,
        db=db
    )
    print(f"Alice linked device 2")
    print(f"  user_id={alice_device2['user_id'][:20]}...")
    print(f"  peer_shared_id={alice_device2['peer_shared_id'][:20]}...")
    db.commit()

    # Verify same user_id
    assert alice_device2['user_id'] == alice_device1['user_id']
    print(f"✅ Both devices share user_id")

    # Sync for link convergence
    print("\n=== Sync for link convergence ===")
    for i in range(15):
        print(f"Sync round {i+1}...")
        tick.tick(t_ms=4000 + i*200, db=db)

    # Verify Alice's second device is also admin
    print("\n=== Verifying device 2 is admin ===")

    is_admin_device2 = invite.is_admin(
        alice_device2['peer_shared_id'],
        alice_device2['peer_id'],
        db
    )
    print(f"Device 2 is admin: {is_admin_device2}")
    assert is_admin_device2, "Device 2 should be admin (linked to admin user)"
    print("✅ Device 2 is admin")

    # Verify device 2 can create invites
    print("\n=== Device 2 creates invite for Bob ===")

    bob_invite_id, bob_invite_link, _ = invite.create(
        peer_id=alice_device2['peer_id'],  # Device 2 creates invite
        t_ms=5000,
        db=db
    )
    print(f"Device 2 created invite: {bob_invite_id[:20]}...")
    print("✅ Device 2 can create invites")
    db.commit()

    # Bob joins via device 2's invite
    print("\n=== Bob joins via device 2's invite ===")

    bob = user.join(
        invite_link=bob_invite_link,
        name='Bob',
        t_ms=6000,
        db=db
    )
    print(f"Bob joined network")
    print(f"  user_id={bob['user_id'][:20]}...")
    db.commit()

    # Sync for Bob's join
    print("\n=== Sync for Bob's join ===")
    for i in range(15):
        print(f"Sync round {i+1}...")
        tick.tick(t_ms=7000 + i*200, db=db)

    # Verify Bob successfully joined
    print("\n=== Verifying Bob's membership ===")

    # Get network's all_users group from Alice's device
    network_row = db.query_one(
        "SELECT all_users_group_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (alice_device1['peer_id'],)
    )
    assert network_row, "Network should exist"
    all_users_group_id = network_row['all_users_group_id']

    # Check if Bob is in all_users group
    bob_is_member = group_member.is_member(
        bob['user_id'],
        all_users_group_id,
        bob['peer_id'],
        db
    )
    print(f"Bob is member of all_users group: {bob_is_member}")
    assert bob_is_member, "Bob should be member of all_users group"
    print("✅ Bob successfully joined via device 2's invite")

    # Verify both Alice devices can see Bob
    print("\n=== Verifying both Alice devices see Bob ===")

    device1_sees_bob = group_member.is_member(
        bob['user_id'],
        all_users_group_id,
        alice_device1['peer_id'],
        db
    )
    print(f"Device 1 sees Bob: {device1_sees_bob}")
    assert device1_sees_bob, "Device 1 should see Bob as member"

    device2_sees_bob = group_member.is_member(
        bob['user_id'],
        all_users_group_id,
        alice_device2['peer_id'],
        db
    )
    print(f"Device 2 sees Bob: {device2_sees_bob}")
    assert device2_sees_bob, "Device 2 should see Bob as member"

    print("✅ Both Alice devices see Bob as member")

    print(f"\n✅ All assertions passed! Admin privileges inherited correctly.")
