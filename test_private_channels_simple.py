#!/usr/bin/env python3
"""Simple test for private channels without complex syncing."""
import sqlite3
from db import Database, create_safe_db
import schema
from events.identity import user, invite
from events.content import channel
from events.group import group_member

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice (admin) and Bob and Charlie (members)
print("=== Creating network ===")
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"✓ Alice created: {alice['user_id']}")

invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"✓ Bob joined: {bob['user_id']}")

charlie = user.join(invite_link=invite_link, name='Charlie', t_ms=2500, db=db)
print(f"✓ Charlie joined: {charlie['user_id']}")

# Test 1: Non-admin cannot create channels
print("\n=== Test 1: Non-admin cannot create channels ===")
try:
    channel.create(
        name="test_channel",
        peer_id=bob['peer_id'],
        peer_shared_id=bob['peer_shared_id'],
        t_ms=3000,
        db=db
    )
    print("❌ FAILURE: Bob (non-admin) was able to create a channel!")
except ValueError as e:
    print(f"✓ SUCCESS: Bob cannot create channel")

# Test 2: Admin can create public channel
print("\n=== Test 2: Admin creates public channel ===")
try:
    public_channel_id = channel.create(
        name="general",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3100,
        db=db
    )
    print(f"✓ SUCCESS: Alice created public channel")
except Exception as e:
    print(f"❌ FAILURE: Alice could not create public channel: {e}")

# Test 3: Admin can create private channel
print("\n=== Test 3: Admin creates private channel with specific members ===")
try:
    private_channel_id = channel.create(
        name="secretgroup",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3200,
        db=db,
        member_user_ids=[bob['user_id']]  # Only Bob
    )
    print(f"✓ SUCCESS: Alice created private channel for Bob")
except Exception as e:
    print(f"❌ FAILURE: Alice could not create private channel: {e}")

# Test 4: Verify private channel group membership (from Alice's perspective)
print("\n=== Test 4: Verify group membership ===")
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
private_ch = alice_safedb.query_one(
    "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
    (private_channel_id, alice['peer_id'])
)

if private_ch:
    group_id = private_ch['group_id']

    # Check if Alice (creator/admin) is a member of the group
    alice_is_member = alice_safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ? AND recorded_by = ?",
        (group_id, alice['user_id'], alice['peer_id'])
    )

    # For now, just verify the group exists and Alice is a member (admins auto-added)
    if alice_is_member:
        print(f"✓ SUCCESS: Alice (admin/creator) is a member of the private group")
    else:
        print(f"❌ FAILURE: Alice is not a member of her own private group")
else:
    print(f"❌ FAILURE: Could not find private channel group")

# Test 5: Add another member to private channel
print("\n=== Test 5: Admin adds Charlie to private channel ===")
try:
    channel.add_member_to_channel(
        channel_id=private_channel_id,
        user_id=charlie['user_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3300,
        db=db
    )
    print(f"✓ SUCCESS: Alice added Charlie to private channel (event created)")
except Exception as e:
    print(f"❌ FAILURE: Could not add Charlie: {e}")

# Test 6: Non-admin cannot add members
print("\n=== Test 6: Non-admin cannot add members ===")
try:
    channel.add_member_to_channel(
        channel_id=private_channel_id,
        user_id=charlie['user_id'],
        peer_id=bob['peer_id'],
        peer_shared_id=bob['peer_shared_id'],
        t_ms=3400,
        db=db
    )
    print("❌ FAILURE: Bob (non-admin) was able to add a member!")
except ValueError as e:
    print(f"✓ SUCCESS: Bob cannot add members")

# Test 7: Verify channel encryption (both channels should use different group keys)
print("\n=== Test 7: Verify channel encryption keys ===")
public_ch = alice_safedb.query_one(
    "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
    (public_channel_id, alice['peer_id'])
)
private_ch = alice_safedb.query_one(
    "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
    (private_channel_id, alice['peer_id'])
)

if public_ch and private_ch:
    if public_ch['group_id'] != private_ch['group_id']:
        print(f"✓ SUCCESS: Public and private channels use different groups")

        # Check if they have different keys
        public_group_key = alice_safedb.query_one(
            "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
            (public_ch['group_id'], alice['peer_id'])
        )
        private_group_key = alice_safedb.query_one(
            "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
            (private_ch['group_id'], alice['peer_id'])
        )

        if public_group_key and private_group_key:
            if public_group_key['key_id'] != private_group_key['key_id']:
                print(f"✓ SUCCESS: Channels use different encryption keys")
            else:
                print(f"❌ FAILURE: Channels use the same encryption key")
    else:
        print(f"❌ FAILURE: Both channels use the same group!")

print("\n=== All tests completed ===")
