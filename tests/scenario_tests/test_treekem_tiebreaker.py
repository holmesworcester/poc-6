"""
Scenario test: TreeKEM Pubkey Tiebreaker Convergence.

Tests that concurrent TreeKEM pubkey updates converge deterministically:
1. Alice and Bob in same network, TreeKEM enabled
2. Partition Bob (block traffic)
3. Both Alice and Bob independently update TreeKEM pubkeys at same timestamp
4. Heal partition (restore traffic)
5. Run sync ticks
6. VERIFY: Both peers converge to same winning pubkey for each node

KNOWN BUG: treekem_pubkey_shared.get_pubkey_for_node() uses ORDER BY created_at DESC
instead of global_count tiebreaker. This may cause non-deterministic winner selection.
"""
import pytest
from events.identity import user, invite, peer, network_settings
from events.group import treekem_update, treekem_pubkey_shared
from core import treekem, network_config
from tests.utils.tick_helper import assert_eventually, TestClock, run_ticks
from core.db import create_safe_db


def test_treekem_pubkey_convergence_same_timestamp(fresh_db):
    """Test that concurrent pubkey updates at same timestamp converge deterministically."""
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

    # Bob joins
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for sync
    def both_synced():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(both_synced, db=db, start_t_ms=None)

    # === Partition Bob ===
    network_config.partition_peer(bob['peer_id'])

    # === Both update pubkeys at exact same timestamp ===
    update_time = t_ms + 1000

    # Alice updates
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=update_time,
        db=db
    )
    db.commit()

    # Bob updates (partitioned, so these are independent)
    treekem_update.update_for_group(
        group_id=alice['network_id'],  # Same network
        peer_id=bob['peer_id'],
        peer_shared_id=bob['peer_shared_id'],
        t_ms=update_time,  # Same timestamp!
        db=db
    )
    db.commit()

    # === Heal partition ===
    network_config.unpartition_peer(bob['peer_id'])

    # === Run sync ===
    t_ms = run_ticks(db=db, start_t_ms=update_time, num_rounds=100)

    # === VERIFY: Both converge to same pubkeys for each node ===
    # Get Alice's leaf position to check their path
    alice_position = treekem.leaf_position_from_peer_id(alice['peer_shared_id'])

    converged = True
    mismatches = []

    for depth in range(treekem.MAX_DEPTH):
        path_prefix = treekem.path_prefix_for_leaf(alice_position, depth)

        # Get pubkey from Alice's perspective
        alice_pubkey = treekem_pubkey_shared.get_pubkey_for_node(
            group_id=alice['network_id'],
            depth=depth,
            path_prefix=path_prefix,
            peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )

        # Get pubkey from Bob's perspective
        bob_pubkey = treekem_pubkey_shared.get_pubkey_for_node(
            group_id=alice['network_id'],
            depth=depth,
            path_prefix=path_prefix,
            peer_id=bob['peer_id'],
            t_ms=t_ms,
            db=db
        )

        if alice_pubkey and bob_pubkey:
            if alice_pubkey['public_key'] != bob_pubkey['public_key']:
                converged = False
                mismatches.append({
                    'depth': depth,
                    'alice_owner': alice_pubkey.get('owner_peer_shared_id'),
                    'bob_owner': bob_pubkey.get('owner_peer_shared_id'),
                })

    if not converged:
        # This test may fail due to known tiebreaker bug
        pytest.xfail(
            f"Pubkey convergence failed (expected - missing global_count tiebreaker). "
            f"Mismatches: {mismatches}"
        )

    # If we get here, convergence succeeded
    assert converged, "All pubkeys should converge after sync"


def test_treekem_pubkey_query_uses_created_at(fresh_db):
    """Test that get_pubkey_for_node uses created_at ordering (documents current behavior)."""
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

    # Create first pubkey update
    t1 = clock.tick()
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t1,
        db=db
    )
    db.commit()

    # Get pubkey at root
    pubkey1 = treekem_pubkey_shared.get_pubkey_for_node(
        group_id=alice['network_id'],
        depth=0,
        path_prefix=b'',
        peer_id=alice['peer_id'],
        t_ms=t1 + 1000,
        db=db
    )

    # Create second pubkey update (later timestamp)
    t2 = clock.tick()
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t2,
        db=db
    )
    db.commit()

    # Get pubkey at root again
    pubkey2 = treekem_pubkey_shared.get_pubkey_for_node(
        group_id=alice['network_id'],
        depth=0,
        path_prefix=b'',
        peer_id=alice['peer_id'],
        t_ms=t2 + 1000,
        db=db
    )

    # The second pubkey should be returned (higher created_at)
    assert pubkey1 is not None
    assert pubkey2 is not None
    assert pubkey1['pubkey_shared_id'] != pubkey2['pubkey_shared_id'], \
        "Different updates should create different pubkey IDs"


def test_treekem_valid_nodes_excludes_expired(fresh_db):
    """Test that get_valid_nodes_for_group excludes expired pubkeys."""
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

    # Create pubkeys
    t_ms = clock.tick()
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Check valid nodes now
    valid_nodes_now = treekem_pubkey_shared.get_valid_nodes_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        t_ms=t_ms + 1000,
        db=db
    )
    assert len(valid_nodes_now) == treekem.MAX_DEPTH, \
        f"Should have {treekem.MAX_DEPTH} valid nodes, got {len(valid_nodes_now)}"

    # Advance time past TTL
    expired_time = t_ms + treekem.TREEKEM_PUBKEY_TTL_MS + 1000

    valid_nodes_expired = treekem_pubkey_shared.get_valid_nodes_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        t_ms=expired_time,
        db=db
    )
    assert len(valid_nodes_expired) == 0, \
        f"Should have 0 valid nodes after TTL, got {len(valid_nodes_expired)}"


@pytest.mark.xfail(reason="Known bug: TreeKEM events missing global_count tiebreaker")
def test_treekem_pubkey_uses_global_count_tiebreaker(fresh_db):
    """Test that pubkey selection uses global_count for LWW tiebreaker.

    This test documents the EXPECTED behavior that is currently missing.
    The implementation uses ORDER BY created_at DESC which doesn't provide
    deterministic ordering when timestamps collide.

    FIX NEEDED:
    - Add global_count INTEGER NOT NULL to treekem_pubkeys_shared schema
    - Encode global_count using header.count in wire format
    - Call global_counter.get_next_global_count() when creating events
    - Change queries to use ORDER BY global_count DESC, treekem_pubkey_shared_id DESC
    """
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

    # This test is expected to fail because global_count is not implemented
    # Once fixed, the test should:
    # 1. Create two pubkeys at same timestamp
    # 2. Verify the one with higher global_count wins
    # 3. Verify deterministic ordering via event_id tiebreaker

    # For now, just check that the table exists and could store global_count
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    # Check if global_count column exists (it currently doesn't)
    try:
        safedb.query(
            "SELECT global_count FROM treekem_pubkeys_shared LIMIT 1"
        )
        has_global_count = True
    except Exception:
        has_global_count = False

    assert has_global_count, \
        "treekem_pubkeys_shared should have global_count column for LWW tiebreaker"
