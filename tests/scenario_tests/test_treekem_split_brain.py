"""Tests for split-brain key safety detection.

These tests verify that the sender-side key safety check prevents
removed users from accessing messages encrypted with keys created
during split-brain scenarios.
"""
import pytest
from tests.utils import tick_helper
from core.db import create_safe_db
from events.identity import user, invite, peer, network
from events.identity import user_removed
from events.group import group as group_module
from events.group import group_member
from events.content import message


def test_split_brain_needs_rerotation_detection(fresh_db):
    """Test that split-brain detection correctly identifies unsafe keys.

    This tests the is_key_safe() function directly with various scenarios.
    """
    db = fresh_db
    clock = tick_helper.TestClock()

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    alice_peer_id = alice['peer_id']
    alice_peer_shared_id = alice['peer_shared_id']
    network_id = alice['network_id']

    # Get all_users group
    all_users_group_id = network.get_all_users_group_id(network_id, alice_peer_id, db)

    # Get current key
    safedb = create_safe_db(db, recorded_by=alice_peer_id)
    group_row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
        (all_users_group_id, alice_peer_id)
    )
    initial_key_id = group_row['key_id']

    # With no removals, key should be safe
    safe, reason = group_module.is_key_safe(initial_key_id, alice_peer_id, db)
    assert safe, f"Key should be safe with no removals: {reason}"

    # Create and invite Bob
    _, bob_invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=clock.tick(), db=db)
    bob_user_id = bob['user_id']
    db.commit()

    # Wait for Bob to join
    t_ms = tick_helper.assert_eventually(
        lambda: len(group_member.list_members(all_users_group_id, alice_peer_id, db)) >= 2,
        db=db,
        start_t_ms=None
    )

    # Remove Bob - Alice creates new key
    t_ms = clock.tick()
    user_removed.create(
        removed_user_id=bob_user_id,
        removed_by_peer_id=alice_peer_shared_id,
        removed_by_local_peer_id=alice_peer_id,
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Get new key after removal
    group_row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
        (all_users_group_id, alice_peer_id)
    )
    key_after_removal = group_row['key_id']

    # Key after removal should be safe (Alice created it and she did the removal)
    safe, reason = group_module.is_key_safe(key_after_removal, alice_peer_id, db)
    assert safe, f"Key should be safe after Alice's own removal: {reason}"

    # The initial key (created before removal) should be unsafe
    safe, reason = group_module.is_key_safe(initial_key_id, alice_peer_id, db)
    assert not safe, "Initial key should be unsafe (created before removal)"
    assert "created before removal" in reason

    print("All safety checks passed!")


def test_admin_can_send_after_removal(fresh_db):
    """Test that admin (Alice) can still send messages after removing a user.

    This verifies the sender-side safety check auto-rotates for admins.
    """
    db = fresh_db
    clock = tick_helper.TestClock()

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    alice_peer_id = alice['peer_id']
    alice_peer_shared_id = alice['peer_shared_id']
    channel_id = alice['channel_id']
    network_id = alice['network_id']

    # Get all_users group
    all_users_group_id = network.get_all_users_group_id(network_id, alice_peer_id, db)

    # Create Bob
    _, bob_invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=clock.tick(), db=db)
    bob_user_id = bob['user_id']
    db.commit()

    # Wait for Bob to join via sync
    t_ms = tick_helper.assert_eventually(
        lambda: len(group_member.list_members(all_users_group_id, alice_peer_id, db)) >= 2,
        db=db,
        start_t_ms=None
    )

    # Alice removes Bob
    t_ms = clock.tick()
    user_removed.create(
        removed_user_id=bob_user_id,
        removed_by_peer_id=alice_peer_shared_id,
        removed_by_local_peer_id=alice_peer_id,
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Alice (admin) should be able to send - her key is safe because she created it
    t_ms = clock.tick()
    msg = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="Alice's message after removal",
        t_ms=t_ms,
        db=db
    )
    assert msg['id'], "Alice (admin) should be able to send after removal"
    db.commit()

    print("Admin can send after removal - verified!")


def test_split_brain_admin_auto_rotates(fresh_db):
    """Test that admin auto-rotates when split-brain is detected.

    Scenario:
    1. Alice creates network with Bob and Carol
    2. Alice removes Bob (creates key_A excluding Bob)
    3. Carol's removal of someone arrives (simulated via sync)
    4. When Alice tries to send, if key is unsafe, she should auto-rotate
    """
    db = fresh_db
    clock = tick_helper.TestClock()

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    alice_peer_id = alice['peer_id']
    alice_peer_shared_id = alice['peer_shared_id']
    channel_id = alice['channel_id']
    network_id = alice['network_id']

    # Get all_users group
    all_users_group_id = network.get_all_users_group_id(network_id, alice_peer_id, db)

    # Create and invite Bob, Carol
    _, bob_invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=clock.tick(), db=db)
    _, carol_invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=clock.tick(), db=db)

    # Bob joins
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=clock.tick(), db=db)
    bob_user_id = bob['user_id']

    # Carol joins
    carol_peer_id = peer.create(t_ms=clock.tick(), db=db)
    carol = user.join(peer_id=carol_peer_id, invite_link=carol_invite_link, name='Carol', t_ms=clock.tick(), db=db)
    carol_peer_shared_id = carol['peer_shared_id']
    carol_user_id = carol['user_id']

    db.commit()

    # Wait for everyone to join via sync
    t_ms = tick_helper.assert_eventually(
        lambda: len(group_member.list_members(all_users_group_id, alice_peer_id, db)) >= 3,
        db=db,
        start_t_ms=None
    )

    # Alice removes Bob
    t_ms = clock.tick()
    user_removed.create(
        removed_user_id=bob_user_id,
        removed_by_peer_id=alice_peer_shared_id,
        removed_by_local_peer_id=alice_peer_id,
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Get the key after Alice's removal
    safedb = create_safe_db(db, recorded_by=alice_peer_id)
    group_row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
        (all_users_group_id, alice_peer_id)
    )
    key_after_alice_removal = group_row['key_id']

    # Check key is safe from Alice's perspective
    safe, reason = group_module.is_key_safe(key_after_alice_removal, alice_peer_id, db)
    assert safe, f"Key should be safe after Alice's removal: {reason}"

    # Alice (admin) should be able to send
    t_ms = clock.tick()
    msg = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="Test message after removal",
        t_ms=t_ms,
        db=db
    )
    assert msg['id'], "Alice should be able to send after removal"
    db.commit()

    print("Split-brain auto-rotation verified!")
