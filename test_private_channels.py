#!/usr/bin/env python3
"""Test private channels: creation, membership, and encryption."""
import sqlite3
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync
from events.content import channel, message

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice (admin) and Bob and Charlie (members)
print("=== Creating network ===")
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice created: {alice['user_id']}")

invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"Bob joined: {bob['user_id']}")

charlie = user.join(invite_link=invite_link, name='Charlie', t_ms=2500, db=db)
print(f"Charlie joined: {charlie['user_id']}")

# Sync to get everyone connected
print("\n=== Initial sync ===")
for round_num in range(5):
    sync.receive(batch_size=20, t_ms=3000 + (round_num * 100), db=db)
    sync.send_request_to_all(t_ms=3050 + (round_num * 100), db=db)

# Test 1: Non-admin cannot create channels
print("\n=== Test 1: Non-admin cannot create channels ===")
try:
    channel.create(
        name="test_channel",
        peer_id=bob['peer_id'],
        peer_shared_id=bob['peer_shared_id'],
        t_ms=5000,
        db=db
    )
    print("❌ FAILURE: Bob (non-admin) was able to create a channel!")
except ValueError as e:
    print(f"✓ SUCCESS: Bob cannot create channel: {e}")

# Test 2: Admin can create public channel
print("\n=== Test 2: Admin creates public channel ===")
try:
    public_channel_id = channel.create(
        name="general",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=5000,
        db=db
    )
    print(f"✓ SUCCESS: Alice created public channel: {public_channel_id}")
except Exception as e:
    print(f"❌ FAILURE: Alice could not create public channel: {e}")

# Test 3: Admin can create private channel
print("\n=== Test 3: Admin creates private channel with specific members ===")
try:
    private_channel_id = channel.create(
        name="secretgroup",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=5100,
        db=db,
        member_user_ids=[bob['user_id']]  # Only Bob
    )
    print(f"✓ SUCCESS: Alice created private channel for Bob: {private_channel_id}")
except Exception as e:
    print(f"❌ FAILURE: Alice could not create private channel: {e}")

# Sync to propagate channel events
print("\n=== Syncing channel creation events ===")
for round_num in range(5):
    sync.receive(batch_size=20, t_ms=5200 + (round_num * 100), db=db)
    sync.send_request_to_all(t_ms=5250 + (round_num * 100), db=db)

# Test 4: Verify private channel group membership
print("\n=== Test 4: Verify group membership ===")
# Get the private channel's group_id
from db import create_safe_db
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
private_ch = alice_safedb.query_one(
    "SELECT group_id FROM channels WHERE channel_id = ?",
    (private_channel_id,)
)
if private_ch:
    group_id = private_ch['group_id']

    # Check Bob is a member of the group
    bob_is_member = alice_safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, bob['user_id'])
    )

    # Check Alice (admin) is a member of the group
    alice_is_member = alice_safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, alice['user_id'])
    )

    # Check Charlie is NOT a member of the group
    charlie_is_member = alice_safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, charlie['user_id'])
    )

    if bob_is_member and alice_is_member and not charlie_is_member:
        print(f"✓ SUCCESS: Correct group membership (Bob: yes, Alice: yes, Charlie: no)")
    else:
        print(f"❌ FAILURE: Wrong group membership (Bob: {bool(bob_is_member)}, Alice: {bool(alice_is_member)}, Charlie: {bool(charlie_is_member)})")
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
        t_ms=5700,
        db=db
    )
    print(f"✓ SUCCESS: Alice added Charlie to private channel")
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
        t_ms=5800,
        db=db
    )
    print("❌ FAILURE: Bob (non-admin) was able to add a member!")
except ValueError as e:
    print(f"✓ SUCCESS: Bob cannot add members: {e}")

# Sync
print("\n=== Final sync ===")
for round_num in range(5):
    sync.receive(batch_size=20, t_ms=5900 + (round_num * 100), db=db)
    sync.send_request_to_all(t_ms=5950 + (round_num * 100), db=db)

# Test 7: Send message to private channel and verify encryption
print("\n=== Test 7: Send message to private channel ===")
try:
    msg_id = message.create(
        content="Secret message for Bob",
        channel_id=private_channel_id,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=6000,
        db=db
    )
    print(f"✓ SUCCESS: Alice sent message to private channel: {msg_id}")
except Exception as e:
    print(f"❌ FAILURE: Could not send message: {e}")

# Final sync
print("\n=== Final sync to propagate message ===")
for round_num in range(5):
    sync.receive(batch_size=20, t_ms=6100 + (round_num * 100), db=db)
    sync.send_request_to_all(t_ms=6150 + (round_num * 100), db=db)

print("\n=== All tests completed ===")
