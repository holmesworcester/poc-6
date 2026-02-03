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
"""
import sqlite3
from core.db import Database
from core import schema
from events.identity import user, invite, peer_shared, peer
from events.group import group, group_member
from tests.utils.tick_helper import assert_eventually, TestClock


def test_link_device_sees_new_groups_after_invite(fresh_db):
    """Second device can see groups created between invite creation and device linking."""

    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network and Group A ===")

    alice_device1 = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    print(f"Alice created network")
    print(f"  user_id={alice_device1['user_id'][:20]}...")
    db.commit()

    group_a_id, _ = group.create(
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

    print("\n=== Alice creates Group B (after invite) ===")

    group_b_id, _ = group.create(
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

    print("\n=== Alice links second device via peer invite ===")

    alice_device2_peer_id = peer.create(t_ms=clock.tick(), db=db)

    accepted = invite.accept(alice_device2_peer_id, invite_link, t_ms=clock.tick(), db=db)
    assert accepted['mode'] == 'peer'

    alice_device2 = peer_shared.join(
        peer_id=alice_device2_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        t_ms=clock.tick(),
        db=db,
        network_id=accepted.get('network_id')
    )
    print(f"Alice linked device 2")
    print(f"  peer_id={alice_device2['peer_id'][:20]}...")
    print(f"  user_id={alice_device2['user_id'][:20]}...")
    db.commit()

    # Verify both devices have same user_id
    assert alice_device2['user_id'] == alice_device1['user_id']
    print(f"✅ Both devices share user_id")

    # Verify device 2 is member of BOTH groups
    print("\n=== Verifying device 2 group memberships ===")

    def device2_is_member_of_both_groups():
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
        assert is_member_a, "Device 2 should be member of Group A (existed before invite)"
        assert is_member_b, "Device 2 should be member of Group B (created after invite)"

    assert_eventually(device2_is_member_of_both_groups, db=db, start_t_ms=None)
    print("✅ Device 2 is member of both groups")

    print(f"\n✅ All assertions passed!")
