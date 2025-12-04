"""
Scenario test: Single player creates invites.

Alice creates her account and then creates:
1. A device invite (mode='peer') - to link another device
2. A user invite (mode='user') - to invite another user to the network

This tests the invite creation flow without requiring multi-party sync.
"""
import sqlite3
from db import Database, create_safe_db
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

    # Validate device invite is stored correctly
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    device_invite_row = safedb.query_one(
        "SELECT invite_id, mode, user_id FROM invites WHERE invite_id = ? AND recorded_by = ?",
        (device_invite_id, alice['peer_id'])
    )

    assert device_invite_row is not None, "Device invite should be stored in invites table"
    assert device_invite_row['mode'] == 'peer', f"Device invite mode should be 'peer', got '{device_invite_row['mode']}'"
    assert device_invite_row['user_id'] == alice['user_id'], "Device invite should reference Alice's user_id"

    print(f"✅ Device invite is valid")
    print(f"   mode={device_invite_row['mode']}")
    print(f"   user_id={device_invite_row['user_id'][:20]}...")

    # Verify device invite is in valid_events
    device_invite_valid = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (device_invite_id, alice['peer_id'])
    )
    assert device_invite_valid is not None, "Device invite should be in valid_events"
    print(f"✅ Device invite is in valid_events")

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

    # Validate user invite is stored correctly
    user_invite_row = safedb.query_one(
        "SELECT invite_id, mode, user_id FROM invites WHERE invite_id = ? AND recorded_by = ?",
        (user_invite_id, alice['peer_id'])
    )

    assert user_invite_row is not None, "User invite should be stored in invites table"
    assert user_invite_row['mode'] == 'user', f"User invite mode should be 'user', got '{user_invite_row['mode']}'"
    assert user_invite_row['user_id'] is None, "User invite should NOT have user_id set (new user will be created)"

    print(f"✅ User invite is valid")
    print(f"   mode={user_invite_row['mode']}")
    print(f"   user_id=None (as expected for user invite)")

    # Verify user invite is in valid_events
    user_invite_valid = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (user_invite_id, alice['peer_id'])
    )
    assert user_invite_valid is not None, "User invite should be in valid_events"
    print(f"✅ User invite is in valid_events")

    # ===== Verify invite data contains expected fields =====
    print("\n=== Verifying invite link data ===")

    # Device invite data should contain user_id (for device linking)
    assert 'invite_blob' in device_invite_data, "Device invite should have invite_blob"
    assert 'invite_id' in device_invite_data, "Device invite should have invite_id"
    assert 'inviter_peer_shared_id' in device_invite_data, "Device invite should have inviter_peer_shared_id"
    assert 'network_id' in device_invite_data, "Device invite should have network_id"
    assert device_invite_data['network_id'] == alice['network_id'], "Device invite network_id should match"
    print(f"✅ Device invite data contains all expected fields")

    # User invite data should have same structure
    assert 'invite_blob' in user_invite_data, "User invite should have invite_blob"
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

    # ===== Count total invites =====
    total_invites = safedb.query_one(
        "SELECT COUNT(*) as count FROM invites WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    # Bootstrap invite + device invite + user invite = 3
    print(f"\n=== Total invites in database: {total_invites['count']} ===")
    assert total_invites['count'] >= 2, f"Should have at least 2 invites (device + user), got {total_invites['count']}"

    print(f"\n✅ All assertions passed! Alice successfully created both device and user invites.")
