"""
Scenario test: Alice adds Bob as admin and both peers converge on Bob's admin status.

Alice creates a network. Bob joins Alice's network via invite.
Alice then adds Bob to the admin group.

Tests:
- Alice is automatically added to admin group during network creation
- Bob joins as regular user (not admin)
- Alice can add Bob to admin group
- Both Alice and Bob converge on Bob's admin status after sync
- Admin-only invite creation enforcement
- Rogue non-admin invites are rejected
"""
import sqlite3
import json
import base64
from db import Database
import schema
from events.identity import user, invite, network, peer, peer_shared
from events.group import group_member
from events.transit import transit_prekey
import tick
import store
import crypto


def test_admin_group_workflow():
    """Test admin group: Alice creates network, Bob joins, Alice makes Bob admin."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Create network and invite ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network, network_id: {alice['network_id'][:20]}...")
    print(f"Alice all_users_group_id: {alice['all_users_group_id'][:20]}...")
    print(f"Alice admins_group_id: {alice['admins_group_id'][:20]}...")

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    print(f"Alice created invite: {invite_id[:20]}...")

    # Bob joins Alice's network
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"Bob joined network, peer_id: {bob['peer_id'][:20]}...")

    db.commit()

    # Initial sync to converge (need multiple rounds for GKS events to propagate)
    for i in range(5):
        print(f"\n=== Sync Round {i+1} ===")
        tick.tick(t_ms=4000 + i*200, db=db)

    # Get admin group ID from network
    admin_group_id = network.get_admin_group_id(alice['network_id'], alice['peer_id'], db)
    print(f"\nAdmin group ID: {admin_group_id[:20]}...")
    assert admin_group_id == alice['admins_group_id'], "Admin group ID should match"

    # Verify Alice is in admin group (added during network.project())
    print("\n=== Verify Alice is admin ===")
    alice_is_admin_alice_view = group_member.is_member(
        alice['user_id'],
        admin_group_id,
        alice['peer_id'],
        db
    )
    print(f"Alice's view: Alice is admin = {alice_is_admin_alice_view}")
    assert alice_is_admin_alice_view, "Alice should be admin (network creator)"

    alice_is_admin_bob_view = group_member.is_member(
        alice['user_id'],
        admin_group_id,
        bob['peer_id'],
        db
    )
    print(f"Bob's view: Alice is admin = {alice_is_admin_bob_view}")
    assert alice_is_admin_bob_view, "Bob should see Alice as admin after sync"

    # Verify Bob is NOT in admin group initially
    print("\n=== Verify Bob is NOT admin initially ===")
    bob_is_admin_alice_view = group_member.is_member(
        bob['user_id'],
        admin_group_id,
        alice['peer_id'],
        db
    )
    print(f"Alice's view: Bob is admin = {bob_is_admin_alice_view}")
    assert not bob_is_admin_alice_view, "Bob should NOT be admin initially"

    bob_is_admin_bob_view = group_member.is_member(
        bob['user_id'],
        admin_group_id,
        bob['peer_id'],
        db
    )
    print(f"Bob's view: Bob is admin = {bob_is_admin_bob_view}")
    assert not bob_is_admin_bob_view, "Bob should NOT see himself as admin initially"

    # Test: Bob tries to add himself as admin (should fail)
    print("\n=== Test: Bob tries to add himself as admin (should fail) ===")
    try:
        group_member.create(
            group_id=admin_group_id,
            user_id=bob['user_id'],
            peer_id=bob['peer_id'],
            peer_shared_id=bob['peer_shared_id'],
            t_ms=4500,
            db=db
        )
        assert False, "Bob should NOT be able to add himself as admin (not authorized)"
    except ValueError as e:
        print(f"✓ Bob correctly prevented from adding himself: {e}")
        assert "not authorized" in str(e).lower(), "Error should mention authorization"

    # Verify Bob is still NOT admin after failed attempt
    bob_is_admin_bob_view = group_member.is_member(
        bob['user_id'],
        admin_group_id,
        bob['peer_id'],
        db
    )
    assert not bob_is_admin_bob_view, "Bob should still NOT be admin after failed attempt"

    # Alice adds Bob to admin group
    print("\n=== Alice adds Bob to admin group ===")
    member_id = group_member.create(
        group_id=admin_group_id,
        user_id=bob['user_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=5000,
        db=db
    )
    print(f"Alice created group_member event: {member_id[:20]}...")
    db.commit()

    # Verify Alice sees Bob as admin immediately
    bob_is_admin_alice_view = group_member.is_member(
        bob['user_id'],
        admin_group_id,
        alice['peer_id'],
        db
    )
    print(f"Alice's view after adding: Bob is admin = {bob_is_admin_alice_view}")
    assert bob_is_admin_alice_view, "Alice should see Bob as admin after adding him"

    # Bob doesn't see it yet (needs sync)
    bob_is_admin_bob_view = group_member.is_member(
        bob['user_id'],
        admin_group_id,
        bob['peer_id'],
        db
    )
    print(f"Bob's view before sync: Bob is admin = {bob_is_admin_bob_view}")
    assert not bob_is_admin_bob_view, "Bob should NOT see himself as admin yet (needs sync)"

    # Sync to propagate admin group membership
    print("\n=== Sync to propagate admin membership ===")
    for round_num in range(3):  # A few rounds to ensure convergence
        tick.tick(t_ms=6000 + round_num * 100, db=db)

    # Verify both peers see Bob as admin
    print("\n=== Verify both peers see Bob as admin after sync ===")
    bob_is_admin_alice_view = group_member.is_member(
        bob['user_id'],
        admin_group_id,
        alice['peer_id'],
        db
    )
    print(f"Alice's view: Bob is admin = {bob_is_admin_alice_view}")
    assert bob_is_admin_alice_view, "Alice should see Bob as admin"

    bob_is_admin_bob_view = group_member.is_member(
        bob['user_id'],
        admin_group_id,
        bob['peer_id'],
        db
    )
    print(f"Bob's view: Bob is admin = {bob_is_admin_bob_view}")
    assert bob_is_admin_bob_view, "Bob should see himself as admin after sync"

    # List all admin group members from both perspectives
    print("\n=== List admin group members ===")
    alice_admins_list = group_member.list_members(admin_group_id, alice['peer_id'], db)
    print(f"Alice's view - admins: {[m['user_id'][:20] + '...' for m in alice_admins_list]}")
    assert len(alice_admins_list) == 2, "Alice should see 2 admins (Alice + Bob)"

    bob_admins_list = group_member.list_members(admin_group_id, bob['peer_id'], db)
    print(f"Bob's view - admins: {[m['user_id'][:20] + '...' for m in bob_admins_list]}")
    assert len(bob_admins_list) == 2, "Bob should see 2 admins (Alice + Bob)"

    # Verify the admin user_ids match
    alice_admin_user_ids = {m['user_id'] for m in alice_admins_list}
    bob_admin_user_ids = {m['user_id'] for m in bob_admins_list}
    assert alice_admin_user_ids == bob_admin_user_ids, "Both peers should agree on admin membership"
    assert alice['user_id'] in alice_admin_user_ids, "Alice should be in admins"
    assert bob['user_id'] in bob_admin_user_ids, "Bob should be in admins"

    # Now Bob (as admin) invites Charlie
    print("\n=== Bob (now admin) invites Charlie ===")
    charlie_invite_id, charlie_invite_link, charlie_invite_data = invite.create(
        peer_id=bob['peer_id'],
        t_ms=7000,
        db=db
    )
    print(f"Bob created invite for Charlie: {charlie_invite_id[:20]}...")

    # Charlie joins via Bob's invite
    charlie_peer_id, charlie_peer_shared_id = peer.create(t_ms=8000, db=db)

    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite_link, name='Charlie', t_ms=8000, db=db)
    print(f"Charlie joined network, peer_id: {charlie['peer_id'][:20]}...")
    db.commit()

    # Sync between all three peers (need more rounds for 3-way sync)
    print("\n=== Sync to integrate Charlie ===")
    for round_num in range(5):  # More rounds for 3-peer convergence
        tick.tick(t_ms=9000 + round_num * 100, db=db)

    # Verify Charlie can see both Alice and Bob as admins
    print("\n=== Verify Charlie sees both Alice and Bob as admins ===")
    charlie_admins_list = group_member.list_members(admin_group_id, charlie['peer_id'], db)
    print(f"Charlie's view - admins: {[m['user_id'][:20] + '...' for m in charlie_admins_list]}")
    assert len(charlie_admins_list) == 2, "Charlie should see 2 admins (Alice + Bob)"

    charlie_admin_user_ids = {m['user_id'] for m in charlie_admins_list}
    assert alice['user_id'] in charlie_admin_user_ids, "Charlie should see Alice as admin"
    assert bob['user_id'] in charlie_admin_user_ids, "Charlie should see Bob as admin"
    assert charlie['user_id'] not in charlie_admin_user_ids, "Charlie should NOT be admin"

    # Test: Rogue non-admin invite (Charlie tries to invite Dave before being admin)
    print("\n=== Test: Rogue non-admin invite (Charlie creates invite without admin permission) ===")

    # Charlie tries to create an invite (should fail - not an admin)
    try:
        invite.create(peer_id=charlie['peer_id'], t_ms=10000, db=db)
        assert False, "Charlie should NOT be able to create invite (not admin)"
    except ValueError as e:
        print(f"✓ Charlie correctly prevented from creating invite: {e}")
        assert "not an admin" in str(e).lower(), "Error should mention admin requirement"

    # Now craft a ROGUE invite: manually create an invite event bypassing validation
    # This simulates a malicious client that bypasses invite.create() checks
    print("\n=== Crafting rogue invite (bypassing normal validation) ===")

    # Get Charlie's info
    from db import create_safe_db, create_unsafe_db
    charlie_safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
    unsafedb = create_unsafe_db(db)

    # Get network info from Charlie's perspective
    charlie_network = charlie_safedb.query_one(
        "SELECT network_id, all_users_group_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (charlie['peer_id'],)
    )

    # Get Charlie's channel
    charlie_channel = charlie_safedb.query_one(
        "SELECT channel_id FROM channels WHERE recorded_by = ? AND is_main = 1 LIMIT 1",
        (charlie['peer_id'],)
    )

    # Get group key
    charlie_group_key_row = charlie_safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (charlie_network['all_users_group_id'], charlie['peer_id'])
    )

    # Create a fake invite prekey for the rogue invite
    rogue_invite_private_key, rogue_invite_public_key = crypto.generate_keypair()
    rogue_invite_pubkey_b64 = crypto.b64encode(rogue_invite_public_key)
    rogue_invite_prekey_id = crypto.b64encode(crypto.hash(rogue_invite_public_key)[:16])

    # Get Charlie's transit prekey for the invite (use unsafedb - device-wide table)
    charlie_prekey_row = unsafedb.query_one(
        "SELECT transit_prekey_id, public_key FROM transit_prekeys WHERE owner_peer_id = ? LIMIT 1",
        (charlie['peer_id'],)
    )

    # Create ROGUE invite event (Charlie is NOT admin, but we're bypassing validation)
    rogue_invite_data = {
        'type': 'invite',
        'invite_pubkey': rogue_invite_pubkey_b64,
        'invite_prekey_id': rogue_invite_prekey_id,
        'network_id': charlie_network['network_id'],
        'group_id': charlie_network['all_users_group_id'],
        'channel_id': charlie_channel['channel_id'],
        'key_id': charlie_group_key_row['key_id'],
        'inviter_peer_shared_id': charlie['peer_shared_id'],
        'inviter_user_id': charlie['user_id'],  # Charlie is NOT an admin!
        'inviter_transit_prekey_public_key': crypto.b64encode(charlie_prekey_row['public_key']),
        'inviter_transit_prekey_shared_id': charlie['peer_shared_id'],  # Simplified
        'inviter_transit_prekey_id': charlie_prekey_row['transit_prekey_id'],
        'created_by': charlie['peer_shared_id'],
        'created_at': 11000
    }

    # Sign with Charlie's key (malicious client has access to their own key)
    charlie_private_key = peer.get_private_key(charlie['peer_id'], charlie['peer_id'], db)
    signed_rogue_invite = crypto.sign_event(rogue_invite_data, charlie_private_key)

    # Store the rogue invite directly (bypassing invite.create() validation)
    rogue_invite_blob = crypto.canonicalize_json(signed_rogue_invite)
    rogue_invite_id = store.event(rogue_invite_blob, charlie['peer_id'], 11000, db)
    print(f"Rogue invite created: {rogue_invite_id[:20]}...")
    db.commit()

    # Sync the rogue invite to Alice (who should reject it)
    print("\n=== Syncing rogue invite to Alice ===")
    for round_num in range(3):
        tick.tick(t_ms=12000 + round_num * 100, db=db)

    # Verify Alice rejected the rogue invite (should not be in her invites table)
    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    alice_rogue_invite = alice_safedb.query_one(
        "SELECT 1 FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
        (rogue_invite_id, alice['peer_id'])
    )
    assert alice_rogue_invite is None, "Alice should reject rogue invite from non-admin Charlie"
    print("✓ Alice correctly rejected rogue invite from non-admin")

    # Verify Bob also rejected it
    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
    bob_rogue_invite = bob_safedb.query_one(
        "SELECT 1 FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
        (rogue_invite_id, bob['peer_id'])
    )
    assert bob_rogue_invite is None, "Bob should reject rogue invite from non-admin Charlie"
    print("✓ Bob correctly rejected rogue invite from non-admin")

    print("\n✅ All assertions passed! Admin group and invite security work correctly.")
