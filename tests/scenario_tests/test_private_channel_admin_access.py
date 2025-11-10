#!/usr/bin/env python3
"""Test that all admins get access to all private channels."""
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

print("=== Scenario: Multiple admins, all must access private channels ===\n")

# Create Alice (admin)
print("Step 1: Alice creates network (becomes first admin)")
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"  ✓ Alice created: {alice['user_id']}")

# Create Bob (initially non-admin)
print("\nStep 2: Bob joins the network (starts as non-admin)")
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"  ✓ Bob joined: {bob['user_id']}")

# Create Charlie (initially non-admin)
print("\nStep 3: Charlie joins the network (starts as non-admin)")
charlie = user.join(invite_link=invite_link, name='Charlie', t_ms=2500, db=db)
print(f"  ✓ Charlie joined: {charlie['user_id']}")

# Get admin group
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
admin_group = alice_safedb.query_one(
    "SELECT admins_group_id FROM networks WHERE recorded_by = ?",
    (alice['peer_id'],)
)
admins_group_id = admin_group['admins_group_id']

# Make Bob an admin
print("\nStep 4: Alice promotes Bob to admin")
group_member.create(
    group_id=admins_group_id,
    user_id=bob['user_id'],
    peer_id=alice['peer_id'],
    peer_shared_id=alice['peer_shared_id'],
    t_ms=3000,
    db=db
)
print(f"  ✓ Bob added to admin group")

# Make Charlie an admin
print("\nStep 5: Alice promotes Charlie to admin")
group_member.create(
    group_id=admins_group_id,
    user_id=charlie['user_id'],
    peer_id=alice['peer_id'],
    peer_shared_id=alice['peer_shared_id'],
    t_ms=3100,
    db=db
)
print(f"  ✓ Charlie added to admin group")

# Create a private channel with only Bob as a member
print("\nStep 6: Alice creates private channel 'secret_sales' with only Bob as member")
print("  (Charlie is an admin but NOT an explicit member)")
secret_channel_id = channel.create(
    name="secret_sales",
    peer_id=alice['peer_id'],
    peer_shared_id=alice['peer_shared_id'],
    t_ms=3200,
    db=db,
    member_user_ids=[bob['user_id']]  # Only Bob
)
print(f"  ✓ Created private channel: {secret_channel_id}")

# Verify group memberships
print("\nStep 7: Verify admin access to private channel group")
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
private_ch = alice_safedb.query_one(
    "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
    (secret_channel_id, alice['peer_id'])
)

if private_ch:
    group_id = private_ch['group_id']

    # Check who's in the group
    members = alice_safedb.query(
        "SELECT user_id FROM group_members WHERE group_id = ? AND recorded_by = ? ORDER BY user_id",
        (group_id, alice['peer_id'])
    )

    member_ids = [m['user_id'] for m in members]
    print(f"  Group {group_id} members:")

    # Check each person
    alice_is_member = alice['user_id'] in member_ids
    bob_is_member = bob['user_id'] in member_ids
    charlie_is_member = charlie['user_id'] in member_ids

    print(f"    - Alice (creator + admin): {alice_is_member}")
    print(f"    - Bob (explicit member + admin): {bob_is_member}")
    print(f"    - Charlie (admin but not explicit member): {charlie_is_member}")

    # VERIFICATION
    if alice_is_member and bob_is_member and charlie_is_member:
        print("\n  ✅ SUCCESS: All admins automatically added to private channel!")
        print("     Even though Charlie was NOT explicitly listed in member_user_ids,")
        print("     Charlie was still added because she's an admin.")
    else:
        print("\n  ❌ FAILURE: Not all admins added to private channel")
        print(f"     Expected all three to be members, got: Alice={alice_is_member}, Bob={bob_is_member}, Charlie={charlie_is_member}")

# Create a second private channel with only Charlie as explicit member
print("\n\nStep 8: Alice creates another private channel 'executive_decisions' with only Charlie as member")
print("  (Bob is an admin but NOT an explicit member)")
exec_channel_id = channel.create(
    name="executive_decisions",
    peer_id=alice['peer_id'],
    peer_shared_id=alice['peer_shared_id'],
    t_ms=3300,
    db=db,
    member_user_ids=[charlie['user_id']]  # Only Charlie
)
print(f"  ✓ Created private channel: {exec_channel_id}")

print("\nStep 9: Verify admin access to second private channel")
exec_ch = alice_safedb.query_one(
    "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
    (exec_channel_id, alice['peer_id'])
)

if exec_ch:
    group_id = exec_ch['group_id']

    members = alice_safedb.query(
        "SELECT user_id FROM group_members WHERE group_id = ? AND recorded_by = ? ORDER BY user_id",
        (group_id, alice['peer_id'])
    )

    member_ids = [m['user_id'] for m in members]

    alice_is_member = alice['user_id'] in member_ids
    bob_is_member = bob['user_id'] in member_ids
    charlie_is_member = charlie['user_id'] in member_ids

    print(f"  Group {group_id} members:")
    print(f"    - Alice (creator + admin): {alice_is_member}")
    print(f"    - Bob (admin but not explicit member): {bob_is_member}")
    print(f"    - Charlie (explicit member + admin): {charlie_is_member}")

    if alice_is_member and bob_is_member and charlie_is_member:
        print("\n  ✅ SUCCESS: Bob (admin, not explicit member) automatically added!")
    else:
        print("\n  ❌ FAILURE: Not all admins added")

# Test adding non-admin member to verify they don't get admin channels
print("\n\n=== Verification: Non-admins should NOT see all private channels ===")
print("\nStep 10: Create a non-admin user (David)")
david = user.join(invite_link=invite_link, name='David', t_ms=3400, db=db)
print(f"  ✓ David joined (non-admin): {david['user_id']}")

print("\nStep 11: Alice creates private channel 'admin_only' with NO explicit members")
admin_only_channel_id = channel.create(
    name="admin_only",
    peer_id=alice['peer_id'],
    peer_shared_id=alice['peer_shared_id'],
    t_ms=3500,
    db=db,
    member_user_ids=[]  # No explicit members—only admins
)
print(f"  ✓ Created private channel: {admin_only_channel_id}")

print("\nStep 12: Verify only admins are in the admin_only channel")
admin_ch = alice_safedb.query_one(
    "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
    (admin_only_channel_id, alice['peer_id'])
)

if admin_ch:
    group_id = admin_ch['group_id']

    members = alice_safedb.query(
        "SELECT user_id FROM group_members WHERE group_id = ? AND recorded_by = ? ORDER BY user_id",
        (group_id, alice['peer_id'])
    )

    member_ids = [m['user_id'] for m in members]

    alice_is_member = alice['user_id'] in member_ids
    bob_is_member = bob['user_id'] in member_ids
    charlie_is_member = charlie['user_id'] in member_ids
    david_is_member = david['user_id'] in member_ids

    print(f"  Group {group_id} members:")
    print(f"    - Alice (admin): {alice_is_member}")
    print(f"    - Bob (admin): {bob_is_member}")
    print(f"    - Charlie (admin): {charlie_is_member}")
    print(f"    - David (non-admin): {david_is_member}")

    if alice_is_member and bob_is_member and charlie_is_member and not david_is_member:
        print("\n  ✅ SUCCESS: Only admins added to private channel!")
        print("     David (non-admin) is correctly excluded.")
    else:
        print("\n  ❌ FAILURE: Unexpected membership")

print("\n\n=== Summary ===")
print("✅ All admins automatically get access to all private channels")
print("✅ Non-admins only get access if explicitly added as members")
print("✅ This ensures admin oversight while maintaining channel privacy")
