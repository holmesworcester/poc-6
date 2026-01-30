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
from core.db import Database
from core import schema
from events.identity import user, invite, peer_shared, peer, network
from events.group import group_member
from tests.utils.tick_helper import assert_eventually, TestClock


def test_linked_device_inherits_admin_privileges(fresh_db):
    """Linked device inherits admin privileges from user."""

    # Setup
    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network (becomes admin) ===")

    # Alice creates network
    alice_device1 = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
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

    invite_id, invite_link, _ = invite.create(
        peer_id=alice_device1['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='peer',
        user_id=alice_device1['user_id']
    )
    db.commit()

    alice_device2_peer_id = peer.create(t_ms=clock.tick(), db=db)
    accepted = invite.accept(alice_device2_peer_id, invite_link, t_ms=clock.tick(), db=db)
    assert accepted['mode'] == 'peer'
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
    print(f"  user_id={alice_device2['user_id'][:20]}...")
    print(f"  peer_shared_id={alice_device2['peer_shared_id'][:20]}...")
    db.commit()

    # Verify same user_id
    assert alice_device2['user_id'] == alice_device1['user_id']
    print(f"✅ Both devices share user_id")

    # Wait for device 2 to see admin status
    print("\n=== Waiting for device 2 to sync admin status ===")

    def device2_is_admin():
        is_admin_device2 = invite.is_admin(
            alice_device2['peer_shared_id'],
            alice_device2['peer_id'],
            db
        )
        assert is_admin_device2, "Device 2 should be admin (linked to admin user)"

    t_ms = assert_eventually(device2_is_admin, db=db, start_t_ms=None)
    print("✅ Device 2 is admin")

    # Verify device 2 can create invites
    print("\n=== Device 2 creates invite for Bob ===")

    bob_invite_id, bob_invite_link, _ = invite.create(
        peer_id=alice_device2['peer_id'],  # Device 2 creates invite
        t_ms=clock.tick(),
        db=db
    )
    print(f"Device 2 created invite: {bob_invite_id[:20]}...")
    print("✅ Device 2 can create invites")
    db.commit()

    # Bob joins via device 2's invite
    print("\n=== Bob joins via device 2's invite ===")

    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(
        peer_id=bob_peer_id,
        invite_link=bob_invite_link,
        name='Bob',
        t_ms=clock.now(),
        db=db
    )
    print(f"Bob joined network")
    print(f"  user_id={bob['user_id'][:20]}...")
    db.commit()

    # Wait for both Alice devices to see Bob
    print("\n=== Waiting for both devices to see Bob ===")

    # Get network's all_users group from Alice's device
    all_users_group_id = network.get_all_users_group_id(
        alice_device1['network_id'],
        alice_device1['peer_id'],
        db
    )

    def both_devices_see_bob():
        device1_sees_bob = group_member.is_member(
            bob['user_id'],
            all_users_group_id,
            alice_device1['peer_id'],
            db
        )
        device2_sees_bob = group_member.is_member(
            bob['user_id'],
            all_users_group_id,
            alice_device2['peer_id'],
            db
        )
        assert device1_sees_bob, "Device 1 should see Bob as member"
        assert device2_sees_bob, "Device 2 should see Bob as member"

    assert_eventually(both_devices_see_bob, db=db, start_t_ms=None)
    print("✅ Both Alice devices see Bob as member")

    print(f"\n✅ All assertions passed! Admin privileges inherited correctly.")
