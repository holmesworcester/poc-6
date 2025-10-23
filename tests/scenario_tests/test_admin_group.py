"""
Scenario test: Alice adds Bob as admin and both peers converge on Bob's admin status.

Alice creates a network. Bob joins Alice's network via invite.
Alice then adds Bob to the admin group.

Tests:
- Alice is automatically added to admin group during network creation
- Bob joins as regular user (not admin)
- Alice can add Bob to admin group
- Both Alice and Bob converge on Bob's admin status after sync
"""
import sqlite3
from db import Database
import schema
from events.identity import user, invite, network
from events.group import group_member
from events.transit import sync


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
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"Bob joined network, peer_id: {bob['peer_id'][:20]}...")

    db.commit()

    # Initial sync to converge (need 2 rounds: round 1 for bootstrap, round 2 for GKS projection)
    print("\n=== Sync Round 1: Bootstrap ===")
    sync.send_request_to_all(t_ms=4000, db=db)
    db.commit()
    sync.receive(batch_size=20, t_ms=4100, db=db)
    db.commit()

    print("\n=== Sync Round 2: GKS projection ===")
    sync.send_request_to_all(t_ms=4200, db=db)
    db.commit()
    sync.receive(batch_size=20, t_ms=4300, db=db)
    db.commit()

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
        sync.send_request_to_all(t_ms=6000 + round_num * 100, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=6050 + round_num * 100, db=db)
        db.commit()

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

    print("\n✅ All assertions passed! Admin group works correctly.")
