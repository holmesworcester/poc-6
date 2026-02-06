"""
Scenario test: TreeKEM Update Job Cadence.

Tests the TreeKEM update job's pubkey refresh logic:
1. Enable TreeKEM for network
2. Check that needs_update() returns True initially
3. Run job -> pubkeys published
4. Check that needs_update() returns False (fresh pubkeys)
5. Advance time past TTL threshold (15 min before expiry)
6. Check that needs_update() returns True again
"""
import pytest
from events.identity import user, network_settings
from events.group import treekem_update, treekem_pubkey_shared
from core import treekem
from tests.utils.tick_helper import TestClock, run_ticks
from core.db import create_safe_db


def test_treekem_needs_update_initially_true(fresh_db):
    """Test that needs_update() returns True when no pubkeys exist."""
    db = fresh_db
    clock = TestClock()

    # Alice creates network
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

    # Initially needs update (no pubkeys published)
    t_ms = clock.tick()
    needs = treekem_update.needs_update(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )

    assert needs is True, "Should need update when no pubkeys exist"


def test_treekem_update_publishes_pubkeys(fresh_db):
    """Test that update_for_group() publishes pubkeys on path."""
    db = fresh_db
    clock = TestClock()

    # Setup
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

    # Run update
    t_ms = clock.tick()
    shared_ids = treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Should have published pubkeys (one per depth on path)
    # Path has MAX_DEPTH nodes
    assert len(shared_ids) == treekem.MAX_DEPTH, \
        f"Should publish {treekem.MAX_DEPTH} pubkeys, got {len(shared_ids)}"

    # Verify pubkeys are in the table
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    count = safedb.query_one(
        "SELECT COUNT(*) as count FROM treekem_pubkeys_shared WHERE group_id = ? AND recorded_by = ?",
        (alice['network_id'], alice['peer_id'])
    )
    assert count['count'] == treekem.MAX_DEPTH


def test_treekem_needs_update_false_after_update(fresh_db):
    """Test that needs_update() returns False after pubkeys are published."""
    db = fresh_db
    clock = TestClock()

    # Setup
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

    # Run update
    t_ms = clock.tick()
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Should not need update now (fresh pubkeys)
    needs = treekem_update.needs_update(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms + 1000,  # Small time advance
        db=db
    )

    assert needs is False, "Should not need update immediately after publishing"


def test_treekem_needs_update_true_near_expiry(fresh_db):
    """Test that needs_update() returns True when approaching TTL."""
    db = fresh_db
    clock = TestClock()

    # Setup
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

    # Run update
    t_ms = clock.tick()
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Advance time to just before refresh threshold
    # TTL = 1 hour, refresh threshold = 15 min before expiry
    # So at t + 45 min, still valid
    time_at_45_min = t_ms + (45 * 60 * 1000)
    needs_at_45 = treekem_update.needs_update(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=time_at_45_min,
        db=db
    )
    assert needs_at_45 is False, "Should not need update at 45 min (before threshold)"

    # Advance past refresh threshold (15 min before TTL = at 46 min+)
    # At t + 46 min, TTL remaining = 14 min < 15 min threshold
    time_at_46_min = t_ms + (46 * 60 * 1000)
    needs_at_46 = treekem_update.needs_update(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=time_at_46_min,
        db=db
    )
    assert needs_at_46 is True, "Should need update when TTL < refresh threshold"


def test_treekem_update_for_all_groups(fresh_db):
    """Test that update_for_all_groups() processes enabled networks."""
    db = fresh_db
    clock = TestClock()

    # Setup
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

    # Run update for all groups
    t_ms = clock.tick()
    stats = treekem_update.update_for_all_groups(
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Should have processed and updated the network
    assert stats['groups_processed'] >= 1
    assert stats['groups_updated'] >= 1
    assert stats['pubkeys_shared'] >= treekem.MAX_DEPTH


def test_treekem_update_skips_disabled_networks(fresh_db):
    """Test that update_for_all_groups() skips networks without TreeKEM enabled."""
    db = fresh_db
    clock = TestClock()

    # Setup - do NOT enable TreeKEM
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Run update for all groups
    t_ms = clock.tick()
    stats = treekem_update.update_for_all_groups(
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Should not update any (TreeKEM not enabled)
    assert stats['groups_updated'] == 0
    assert stats['pubkeys_shared'] == 0


def test_treekem_is_enabled_check(fresh_db):
    """Test is_treekem_enabled() function."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Initially disabled
    enabled = treekem_update.is_treekem_enabled(
        alice['network_id'], alice['peer_id'], db
    )
    assert not enabled, "TreeKEM should be disabled by default"

    # Enable it
    network_settings.set_treekem_enabled(
        network_id=alice['network_id'],
        enabled=True,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Now enabled
    enabled = treekem_update.is_treekem_enabled(
        alice['network_id'], alice['peer_id'], db
    )
    assert enabled, "TreeKEM should be enabled after setting"

    # Disable it
    network_settings.set_treekem_enabled(
        network_id=alice['network_id'],
        enabled=False,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Now disabled again
    enabled = treekem_update.is_treekem_enabled(
        alice['network_id'], alice['peer_id'], db
    )
    assert not enabled, "TreeKEM should be disabled after unsetting"


def test_treekem_pubkeys_have_correct_ttl(fresh_db):
    """Test that published pubkeys have the expected TTL."""
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

    # Publish pubkeys
    t_ms = clock.tick()
    treekem_update.update_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Check pubkey TTLs
    pubkeys = treekem_pubkey_shared.list_for_group(
        group_id=alice['network_id'],
        peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )

    assert len(pubkeys) > 0, "Should have pubkeys"

    for pubkey in pubkeys:
        expected_ttl = t_ms + treekem.TREEKEM_PUBKEY_TTL_MS
        # Allow some slack for timestamp offsets during creation
        assert abs(pubkey['ttl_ms'] - expected_ttl) < 1000, \
            f"Pubkey TTL should be ~{expected_ttl}, got {pubkey['ttl_ms']}"
        assert pubkey['valid'] is True, "Pubkey should be valid immediately after creation"
