"""Debug: What's in the link URL that Device 2 receives?"""
import sqlite3
import base64
import json
from db import Database, create_unsafe_db, create_safe_db
import schema
from events.identity import user, link_invite, link
from events.group import group, group_member
import store
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Setup: Device 1 creates network ===")
alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Device 1 peer_id: {alice_device1['peer_id'][:20]}...")
print(f"Device 1 peer_shared_id: {alice_device1['peer_shared_id'][:20]}...")
db.commit()

# Create groups (to match the test scenario)
group_a_id, group_a_key_id = group.create(
    name='Group A',
    peer_id=alice_device1['peer_id'],
    peer_shared_id=alice_device1['peer_shared_id'],
    t_ms=2000,
    db=db
)
group_member.create(
    group_id=group_a_id,
    user_id=alice_device1['user_id'],
    peer_id=alice_device1['peer_id'],
    peer_shared_id=alice_device1['peer_shared_id'],
    t_ms=2001,
    db=db
)
db.commit()

print("\n=== Device 1 creates link invite ===")
invite_id, invite_link, invite_data = link_invite.create(
    peer_id=alice_device1['peer_id'],
    t_ms=3000,
    db=db
)
print(f"Link invite ID: {invite_id[:20]}...")
print(f"Link URL: {invite_link[:80]}...")
db.commit()

# Create Group B AFTER invite (like in the test)
print("\n=== Device 1 creates Group B (after invite) ===")
group_b_id, group_b_key_id = group.create(
    name='Group B',
    peer_id=alice_device1['peer_id'],
    peer_shared_id=alice_device1['peer_shared_id'],
    t_ms=3500,
    db=db
)
group_member.create(
    group_id=group_b_id,
    user_id=alice_device1['user_id'],
    peer_id=alice_device1['peer_id'],
    peer_shared_id=alice_device1['peer_shared_id'],
    t_ms=3501,
    db=db
)
db.commit()

# Parse the link URL to see what's in it
link_code = invite_link.replace('quiet://link/', '')
padding = (4 - len(link_code) % 4) % 4
link_code_padded = link_code + ('=' * padding)
link_json = base64.urlsafe_b64decode(link_code_padded).decode()
link_data_parsed = json.loads(link_json)

print(f"\n=== Link URL contents (keys): {list(link_data_parsed.keys())}")

# Check if invite_blob is present
if 'invite_blob' in link_data_parsed:
    print(f"✓ invite_blob present in link URL")

    # Decode the invite blob to see what's in it
    invite_blob_b64 = link_data_parsed['invite_blob']
    invite_blob = base64.urlsafe_b64decode(invite_blob_b64 + '===')
    invite_event = crypto.parse_json(invite_blob)

    print(f"\n=== Invite event contents (keys): {list(invite_event.keys())}")
    print(f"  created_by: {invite_event.get('created_by', 'MISSING')[:20]}...")
    print(f"  inviter_peer_shared_id: {invite_event.get('inviter_peer_shared_id', 'MISSING')[:20]}...")

    # Check if created_by matches Device 1's peer_shared_id
    if invite_event.get('created_by') == alice_device1['peer_shared_id']:
        print(f"  ✓ created_by matches Device 1's peer_shared_id")
    else:
        print(f"  ✗ created_by does NOT match Device 1's peer_shared_id")
        print(f"    Expected: {alice_device1['peer_shared_id'][:20]}...")
        print(f"    Got: {invite_event.get('created_by', 'MISSING')[:20]}...")

    if invite_event.get('inviter_peer_shared_id') == alice_device1['peer_shared_id']:
        print(f"  ✓ inviter_peer_shared_id matches Device 1's peer_shared_id")
    else:
        print(f"  ✗ inviter_peer_shared_id does NOT match Device 1's peer_shared_id")
        print(f"    Expected: {alice_device1['peer_shared_id'][:20]}...")
        print(f"    Got: {invite_event.get('inviter_peer_shared_id', 'MISSING')[:20]}...")
else:
    print(f"✗ invite_blob NOT present in link URL")

# Check if inviter_peer_shared_blob is present
if 'inviter_peer_shared_blob' in link_data_parsed:
    print(f"\n✓ inviter_peer_shared_blob present in link URL")
else:
    print(f"\n✗ inviter_peer_shared_blob NOT present in link URL")

print("\n=== Now Device 2 joins via link.join() ===")
alice_device2 = link.join(link_url=invite_link, t_ms=5000, db=db)
print(f"Device 2 peer_id: {alice_device2['peer_id'][:20]}...")
print(f"Device 2 peer_shared_id: {alice_device2['peer_shared_id'][:20]}...")
db.commit()

# Check Device 2's invite_accepteds table
print("\n=== Device 2's invite_accepteds table ===")
safedb2 = create_safe_db(db, recorded_by=alice_device2['peer_id'])
dev2_accepteds = safedb2.query(
    "SELECT invite_id, inviter_peer_shared_id FROM invite_accepteds WHERE recorded_by = ?",
    (alice_device2['peer_id'],)
)
print(f"Device 2 has {len(dev2_accepteds)} invite_accepteds:")
for row in dev2_accepteds:
    inviter = row['inviter_peer_shared_id']
    print(f"  invite={row['invite_id'][:20]}... inviter={inviter[:20] if inviter else 'NULL'}...")
    if inviter == alice_device1['peer_shared_id']:
        print(f"    ✓ This is DEVICE1 (correct!)")
    elif inviter == alice_device2['peer_shared_id']:
        print(f"    ✗ This is DEVICE2 (WRONG - should be Device 1)")
    elif not inviter:
        print(f"    ✗ inviter_peer_shared_id is NULL!")
