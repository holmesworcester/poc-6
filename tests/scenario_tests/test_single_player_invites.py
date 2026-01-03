"""
Scenario test: Single player creates invites.

Alice creates her account and then creates:
1. A device invite (mode='peer') - to link another device
2. A user invite (mode='user') - to invite another user to the network

This tests the invite creation flow without requiring multi-party sync.
"""
import sqlite3
from db import Database
import schema
from events.identity import user, invite, peer


def test_alice_creates_device_and_user_invites(fresh_db):
    """Alice creates her account, then creates both device and user invites."""

    db = fresh_db

    print("\n=== Alice creates her network ===")

    # Alice creates her network (becomes first user and admin)
    alice = user.new_network(name='Alice', t_ms=1000, db=db, device_name='Phone')
    db.commit()

    print(f"Alice created network")
    print(f"  peer_id={alice['peer_id'][:20]}...")
    print(f"  user_id={alice['user_id'][:20]}...")
    print(f"  network_id={alice['network_id'][:20]}...")

    # Verify Alice's components were created
    assert len(alice['peer_id']) == 24
    assert len(alice['user_id']) == 24
    assert len(alice['network_id']) == 24
    assert len(alice['channel_id']) == 24

    # ===== Create device invite (mode='peer') =====
    print("\n=== Alice creates device invite (mode='peer') ===")

    device_invite_id, device_invite_link, device_invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db,
        mode='peer',
        user_id=alice['user_id']
    )
    db.commit()

    print(f"Device invite created: {device_invite_id[:20]}...")
    print(f"Device invite link: {device_invite_link[:60]}...")

    # Validate device invite returned data (what the UI receives)
    assert len(device_invite_id) == 24, "Device invite ID should be 24 chars"
    assert device_invite_link.startswith("quiet://link/"), "Device invite link should use 'link' prefix"
    assert device_invite_data['invite_id'] == device_invite_id, "Returned invite_id should match"
    assert device_invite_data.get('user_id') == alice['user_id'], "Device invite should reference Alice's user_id"

    print(f"✅ Device invite is valid")
    print(f"   user_id={device_invite_data['user_id'][:20]}...")

    # ===== Create user invite (mode='user') =====
    print("\n=== Alice creates user invite (mode='user') ===")

    user_invite_id, user_invite_link, user_invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=3000,
        db=db,
        mode='user'
    )
    db.commit()

    print(f"User invite created: {user_invite_id[:20]}...")
    print(f"User invite link: {user_invite_link[:60]}...")

    # Validate user invite returned data
    assert len(user_invite_id) == 24, "User invite ID should be 24 chars"
    assert user_invite_link.startswith("quiet://invite/"), "User invite link should use 'invite' prefix"
    assert user_invite_data['invite_id'] == user_invite_id, "Returned invite_id should match"
    assert 'user_id' not in user_invite_data, "User invite should NOT have user_id (new user will be created)"

    print(f"✅ User invite is valid")
    print(f"   user_id=None (as expected for user invite)")

    # ===== Verify invite data contains expected fields =====
    print("\n=== Verifying invite link data ===")

    # Device invite data should contain expected fields
    assert 'invite_id' in device_invite_data, "Device invite should have invite_id"
    assert 'inviter_peer_shared_id' in device_invite_data, "Device invite should have inviter_peer_shared_id"
    assert 'network_id' in device_invite_data, "Device invite should have network_id"
    assert device_invite_data['network_id'] == alice['network_id'], "Device invite network_id should match"
    print(f"✅ Device invite data contains all expected fields")

    # User invite data should have same structure
    assert 'invite_id' in user_invite_data, "User invite should have invite_id"
    assert 'inviter_peer_shared_id' in user_invite_data, "User invite should have inviter_peer_shared_id"
    assert 'network_id' in user_invite_data, "User invite network_id should match"
    assert user_invite_data['network_id'] == alice['network_id'], "User invite network_id should match"
    print(f"✅ User invite data contains all expected fields")

    # ===== Verify invites are different =====
    print("\n=== Verifying invites are distinct ===")

    assert device_invite_id != user_invite_id, "Device and user invites should have different IDs"
    assert device_invite_link != user_invite_link, "Device and user invite links should be different"
    print(f"✅ Invites are distinct")

    print(f"\n✅ All assertions passed! Alice successfully created both device and user invites.")
