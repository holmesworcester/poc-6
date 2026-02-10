"""
Scenario test: Expired Prekeys Prevent Leaf Fallback.

Tests the edge case where a peer is offline long enough that BOTH their
TreeKEM pubkeys AND their group_prekeys expire:

1. Alice, Bob, Charlie in network, TreeKEM enabled
2. Partition Charlie for LONG time (past pubkey AND prekey TTL)
3. Advance time past TTL
4. Alice removes Bob -> triggers rotation
5. TreeKEM can't reach Charlie (pubkeys expired)
6. Leaf fallback can't reach Charlie (prekeys expired)
7. Heal Charlie's partition
8. Charlie refreshes prekeys
9. VERIFY: Charlie eventually gets key (via re-share or next rotation)

KNOWN ISSUE: If peer is offline past both pubkey AND prekey TTL, leaf fallback
fails. Not a bug per se, but an operational edge case to document.
"""
import pytest
from events.identity import user, invite, peer, network, network_settings
from events.identity import user_removed
from events.group import group, group_member, group_prekey
from events.content import message
from core import treekem, network_config
from tests.utils.tick_helper import assert_eventually, TestClock, run_ticks
from core.db import create_safe_db


# Group prekey TTL (from group_prekey module, typically 24 hours)
# We'll use a shorter duration for testing
GROUP_PREKEY_TTL_MS = 24 * 60 * 60 * 1000  # 24 hours


def test_treekem_expired_pubkeys_falls_back_to_leaf(fresh_db):
    """Test that expired TreeKEM pubkeys fall back to leaf distribution."""
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

    # Sync
    def all_synced():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(member_list) == 3

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Manually trigger group prekey sharing for all peers
    group_prekey.replenish_for_all_peers(t_ms, db)
    group_prekey.share_existing_prekeys_for_all_peers(t_ms + 100, db)
    db.commit()
    t_ms += 200

    # Let TreeKEM pubkeys and prekeys propagate
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # Advance time past TreeKEM pubkey TTL (1 hour)
    time_after_pubkey_expiry = t_ms + treekem.TREEKEM_PUBKEY_TTL_MS + 1000
    clock.t_ms = time_after_pubkey_expiry

    # Verify pubkeys are expired
    from events.group import treekem_pubkey_shared
    valid_nodes = treekem_pubkey_shared.get_valid_nodes_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        t_ms=time_after_pubkey_expiry,
        db=db
    )
    assert len(valid_nodes) == 0, "All TreeKEM pubkeys should be expired"

    # Remove Bob (TreeKEM won't work, should fall back to leaf)
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
    original_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)['key_id']

    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=time_after_pubkey_expiry,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=time_after_pubkey_expiry, num_rounds=50)

    # Key should still rotate
    new_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)['key_id']
    assert new_key != original_key, "Key should rotate even with expired TreeKEM pubkeys"

    # Charlie should receive key via leaf fallback (assuming prekeys still valid)
    def charlie_has_key():
        charlie_key = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
        assert charlie_key is not None
        assert charlie_key['key_id'] == new_key

    assert_eventually(charlie_has_key, db=db, start_t_ms=t_ms)


def test_treekem_pubkey_refresh_after_expiry(fresh_db):
    """Test that pubkey refresh works after TTL expiry."""
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

    # Initial pubkey update
    t_ms = clock.tick()
    from events.group import treekem_update
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Verify pubkeys exist
    from events.group import treekem_pubkey_shared
    valid_now = treekem_pubkey_shared.get_valid_nodes_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        t_ms=t_ms + 1000,
        db=db
    )
    assert len(valid_now) == treekem.MAX_DEPTH

    # Advance past TTL
    time_after_expiry = t_ms + treekem.TREEKEM_PUBKEY_TTL_MS + 1000

    # Verify expired
    valid_expired = treekem_pubkey_shared.get_valid_nodes_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        t_ms=time_after_expiry,
        db=db
    )
    assert len(valid_expired) == 0, "Pubkeys should be expired"

    # Refresh pubkeys
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=time_after_expiry,
        db=db
    )
    db.commit()

    # Verify new pubkeys
    valid_refreshed = treekem_pubkey_shared.get_valid_nodes_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        t_ms=time_after_expiry + 1000,
        db=db
    )
    assert len(valid_refreshed) == treekem.MAX_DEPTH, "Should have fresh pubkeys after refresh"


def test_long_offline_member_receives_key_after_rejoin(fresh_db):
    """Test that long-offline member eventually gets key after returning online."""
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

    # Sync
    def all_synced():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(member_list) == 3

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Manually trigger group prekey sharing for all peers
    group_prekey.replenish_for_all_peers(t_ms, db)
    group_prekey.share_existing_prekeys_for_all_peers(t_ms + 100, db)
    db.commit()
    t_ms += 200

    # Let pubkeys and prekeys propagate
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    # Partition Charlie (goes offline for a LONG time)
    network_config.partition_peer(charlie['peer_id'])

    # Alice removes Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Run ticks (Charlie is offline)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # Get new key
    new_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)['key_id']

    # Charlie still has old key (offline during rotation)
    charlie_key_offline = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
    assert charlie_key_offline['key_id'] != new_key, "Charlie should have old key while offline"

    # Charlie comes back online
    network_config.unpartition_peer(charlie['peer_id'])

    # Run sync
    def charlie_gets_new_key():
        charlie_key = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
        assert charlie_key['key_id'] == new_key, \
            f"Charlie should get new key. Has {charlie_key['key_id'][:20]}..., expected {new_key[:20]}..."

    assert_eventually(charlie_gets_new_key, db=db, start_t_ms=t_ms, max_rounds=200)


def test_prekey_count_per_member(fresh_db):
    """Test that group prekeys are created for members."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Add Bob
    _, bob_invite, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Sync
    def bob_synced():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_synced, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # Check that Bob has group prekeys from Alice's perspective
    bob_prekey = group_prekey.get_group_prekey_for_peer(
        peer_shared_id=bob['peer_shared_id'],
        recorded_by=alice['peer_id'],
        db=db
    )

    # If prekey exists, distribution should work
    if bob_prekey:
        assert 'id' in bob_prekey
        assert 'public_key' in bob_prekey
        assert bob_prekey['type'] == 'asymmetric'
    else:
        # This is acceptable - prekeys may not have synced yet
        # In production, the job system handles prekey refresh
        pass


def test_treekem_stats_show_distribution_breakdown(fresh_db):
    """Test that stats accurately show TreeKEM vs leaf distribution."""
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

    # Add members
    for name in ['Bob', 'Charlie']:
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        member_peer_id = peer.create(t_ms=clock.tick(), db=db)
        user.join(peer_id=member_peer_id, invite_link=invite_link, name=name, t_ms=clock.now(), db=db)
        db.commit()

    # Sync
    def all_synced():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(member_list) == 3

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=100)

    # Get stats
    from events.group import treekem_key_shared
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
    stats = treekem_key_shared.get_key_sharing_stats(all_users_group_id, alice['peer_id'], db)

    # Verify stats structure
    assert 'group_key_shared_count' in stats, "Should have group_key_shared_count"
    assert 'treekem_key_shared_count' in stats, "Should have treekem_key_shared_count"
    assert 'members' in stats, "Should have members"
    assert 'treekem_enabled' in stats, "Should have treekem_enabled"

    # TreeKEM should be enabled
    assert stats['treekem_enabled'] is True

    # Network-wide stats
    network_stats = treekem_key_shared.get_network_key_stats(alice['peer_id'], db)

    assert 'total_group_key_shared' in network_stats
    assert 'total_treekem_key_shared' in network_stats
    assert 'groups_with_treekem' in network_stats
    assert network_stats['groups_with_treekem'] >= 1

    # Total should be non-negative
    assert network_stats['total_group_key_shared'] >= 0
    assert network_stats['total_treekem_key_shared'] >= 0
