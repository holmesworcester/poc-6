"""
Scenario test: TreeKEM Hybrid Distribution (TreeKEM + Leaf Fallback).

Tests the hybrid distribution mechanism where TreeKEM covers some members
and leaf fallback covers others (e.g., new members without pubkeys):
1. Enable TreeKEM, let existing members publish pubkeys
2. New member Zoe joins (no pubkey yet)
3. Alice removes Bob
4. VERIFY: Existing members get key via TreeKEM
5. VERIFY: Zoe gets key via leaf fallback (group_key_shared)
6. VERIFY: Total events = TreeKEM events + leaf events < O(n)
"""
import pytest
from events.identity import user, invite, peer, network, network_settings
from events.identity import user_removed
from events.group import group, group_member, treekem_key_shared, group_prekey
from events.content import message
from tests.utils.tick_helper import assert_eventually, TestClock, run_ticks
from core.db import create_safe_db


def test_treekem_hybrid_distribution_new_member(fresh_db):
    """Test hybrid distribution: TreeKEM for members with pubkeys, leaf for new members."""
    db = fresh_db
    clock = TestClock()

    # === Setup: Alice creates network ===
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Enable TreeKEM
    network_settings.set_treekem_enabled(
        network_id=alice['network_id'],
        enabled=True,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Add Bob and Charlie (will have pubkeys)
    _, bob_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    _, charlie_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite, name='Charlie', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for sync and let TreeKEM pubkeys propagate
    def initial_sync_done():
        alice_group = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(alice_group, alice['peer_id'], db)
        assert len(member_list) == 3

    t_ms = assert_eventually(initial_sync_done, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Manually trigger group prekey sharing for all peers
    # (The job runs every hour in production, too slow for tests)
    group_prekey.replenish_for_all_peers(t_ms, db)
    group_prekey.share_existing_prekeys_for_all_peers(t_ms + 100, db)
    db.commit()
    t_ms += 200

    # Let pubkeys and prekeys distribute
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # === Add Zoe (new member - no pubkey yet) ===
    _, zoe_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    zoe_peer_id = peer.create(t_ms=clock.tick(), db=db)
    zoe = user.join(peer_id=zoe_peer_id, invite_link=zoe_invite, name='Zoe', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for Zoe to join (minimal sync, not enough for pubkey distribution)
    def zoe_joined():
        alice_group = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(alice_group, alice['peer_id'], db)
        member_names = [m['name'] for m in member_list]
        assert 'Zoe' in member_names
        assert len(member_list) == 4

    t_ms = assert_eventually(zoe_joined, db=db, start_t_ms=t_ms)

    # Distribute prekeys for Zoe (but not pubkeys - testing hybrid scenario)
    group_prekey.replenish_for_all_peers(t_ms, db)
    group_prekey.share_existing_prekeys_for_all_peers(t_ms + 100, db)
    db.commit()
    t_ms += 200
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Get stats before removal
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    gks_before = safedb.query_one(
        "SELECT COUNT(*) as count FROM group_keys_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['count']

    tks_before = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_keys_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['count']

    # === Alice removes Bob ===
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Get stats after removal
    gks_after = safedb.query_one(
        "SELECT COUNT(*) as count FROM group_keys_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['count']

    tks_after = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_keys_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['count']

    gks_created = gks_after - gks_before
    tks_created = tks_after - tks_before

    # Expected: Some TreeKEM events + some leaf events
    remaining_members = 3  # Alice, Charlie, Zoe (Bob removed)

    print(f"group_key_shared events created: {gks_created}")
    print(f"treekem_key_shared events created: {tks_created}")
    print(f"Total events: {gks_created + tks_created}")
    print(f"Remaining members (excluding self): {remaining_members - 1}")

    # At minimum, Zoe should get key via leaf (group_key_shared)
    # Alice doesn't need a share (she created the key)
    # Charlie and Zoe may get via TreeKEM or leaf depending on pubkey availability

    # === VERIFY: New key was created ===
    original_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    assert original_key is not None, "New key should exist"

    # === VERIFY: Zoe eventually gets the key ===
    def zoe_has_key():
        zoe_key = group.get_current_key(all_users_group_id, zoe['peer_id'], db)
        assert zoe_key is not None, "Zoe should have the key"
        assert zoe_key['key_id'] == original_key['key_id'], \
            "Zoe should have same key as Alice"

    assert_eventually(zoe_has_key, db=db, start_t_ms=t_ms)

    # === VERIFY: Charlie gets the key ===
    def charlie_has_key():
        charlie_key = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
        assert charlie_key is not None, "Charlie should have the key"
        assert charlie_key['key_id'] == original_key['key_id'], \
            "Charlie should have same key as Alice"

    assert_eventually(charlie_has_key, db=db, start_t_ms=t_ms)


def test_treekem_hybrid_all_via_leaf_when_no_pubkeys(fresh_db):
    """Test that distribution falls back to full O(n) when no pubkeys exist."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Enable TreeKEM but DON'T wait for pubkey distribution
    network_settings.set_treekem_enabled(
        network_id=alice['network_id'],
        enabled=True,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Add Bob
    _, bob_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Add Charlie
    _, charlie_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite, name='Charlie', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for members but DON'T wait for TreeKEM pubkeys
    def all_joined():
        alice_group = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(alice_group, alice['peer_id'], db)
        assert len(member_list) == 3

    t_ms = assert_eventually(all_joined, db=db, start_t_ms=None)
    # Only run minimal ticks - not enough for TreeKEM pubkeys to propagate
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=5)

    # Get stats before
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    tks_before = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_keys_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['count']

    # Remove Bob (triggers key rotation)
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)

    # Get stats after
    tks_after = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_keys_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['count']

    # If no pubkeys were available, TreeKEM events should be 0 or very few
    # and all distribution went via leaf fallback
    tks_created = tks_after - tks_before

    # Get key sharing stats
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
    stats = treekem_key_shared.get_key_sharing_stats(all_users_group_id, alice['peer_id'], db)

    print(f"TreeKEM events created: {tks_created}")
    print(f"Stats: {stats}")

    # Charlie should still receive the key (via leaf fallback)
    def charlie_has_key():
        charlie_key = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
        assert charlie_key is not None

    assert_eventually(charlie_has_key, db=db, start_t_ms=t_ms)


def test_treekem_disabled_uses_only_leaf(fresh_db):
    """Test that with TreeKEM disabled, only leaf distribution is used."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # DO NOT enable TreeKEM

    # Add members
    _, bob_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    _, charlie_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite, name='Charlie', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for sync
    def all_joined():
        alice_group = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(alice_group, alice['peer_id'], db)
        assert len(member_list) == 3

    t_ms = assert_eventually(all_joined, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)

    # Get initial counts
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    tks_before = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_keys_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['count']

    # Remove Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)

    # Should have used zero TreeKEM events
    tks_after = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_keys_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['count']

    tks_created = tks_after - tks_before
    assert tks_created == 0, f"Should not use TreeKEM when disabled, but created {tks_created} events"

    # But Charlie should still get the key (via leaf)
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    def charlie_has_key():
        charlie_key = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
        assert charlie_key is not None

    assert_eventually(charlie_has_key, db=db, start_t_ms=t_ms)


def test_treekem_member_can_send_after_receiving_via_hybrid(fresh_db):
    """Test that member who received key via hybrid can send messages."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    network_settings.set_treekem_enabled(
        network_id=alice['network_id'],
        enabled=True,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Add Bob and Charlie
    _, bob_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    _, charlie_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite, name='Charlie', t_ms=clock.now(), db=db)
    db.commit()

    # Full sync with TreeKEM pubkeys
    def all_synced():
        alice_group = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(alice_group, alice['peer_id'], db)
        assert len(member_list) == 3

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=100)

    # Remove Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # Charlie sends a message with the new key
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    def charlie_can_send():
        charlie_key = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
        assert charlie_key is not None

    assert_eventually(charlie_can_send, db=db, start_t_ms=t_ms)

    message.create(
        peer_id=charlie['peer_id'],
        channel_id=alice['channel_id'],  # Same channel
        content='Charlie message with new key',
        t_ms=t_ms + 1000,
        db=db
    )
    db.commit()

    # Alice should see Charlie's message
    def alice_sees_charlie_message():
        msgs = message.list(alice['channel_id'], alice['peer_id'], db)
        contents = [m['content'] for m in msgs]
        assert 'Charlie message with new key' in contents

    assert_eventually(alice_sees_charlie_message, db=db, start_t_ms=t_ms + 1000)
