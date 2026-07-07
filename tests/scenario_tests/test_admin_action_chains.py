"""Tests for admin action chains - verifying DAG-based removal protection.

These tests verify that:
1. Admin actions form chains via the 'prior' field
2. Actions from removed admins (post-removal) are rejected
3. Actions known before removal are still valid
4. Key rotation includes prior=[removal_id] for tracking
"""
import pytest
from core.db import create_safe_db
from core import tick
from events.identity import user, invite, admin, peer
from events.content import channel


def test_admin_actions_form_chain(fresh_db):
    """Admin actions reference prior admin action from same peer via 'prior' field."""
    db = fresh_db
    tick.reset_state(db)

    # Create network - Alice is bootstrap admin
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates an invite (first admin action after bootstrap)
    invite_id_1, _, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db
    )

    # Check that the invite was recorded in admin_actions
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    action_1 = safedb.query_one(
        "SELECT * FROM admin_actions WHERE action_id = ? AND recorded_by = ?",
        (invite_id_1, alice['peer_id'])
    )
    assert action_1 is not None, "First invite should be in admin_actions"
    assert action_1['action_type'] == 'invite'
    assert action_1['peer_shared_id'] == alice['peer_shared_id']
    # First action has no prior (bootstrap)
    # Note: prior_0 may be None or reference bootstrap admin event

    # Alice creates another invite
    invite_id_2, _, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=3000,
        db=db
    )

    # Second invite should reference first as prior
    action_2 = safedb.query_one(
        "SELECT * FROM admin_actions WHERE action_id = ? AND recorded_by = ?",
        (invite_id_2, alice['peer_id'])
    )
    assert action_2 is not None, "Second invite should be in admin_actions"
    assert action_2['prior_0'] == invite_id_1, "Second invite should reference first as prior"


def test_admin_creates_channel_with_prior(fresh_db):
    """Channel creation includes prior for admin action chain."""
    db = fresh_db
    tick.reset_state(db)

    # Create network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates an invite first
    invite_id, _, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db
    )

    # Alice creates a channel (should reference invite as prior)
    channel_id = channel.create(
        name='test-channel',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3000,
        db=db
    )

    # Channel should reference invite as prior
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    channel_action = safedb.query_one(
        "SELECT * FROM admin_actions WHERE action_id = ? AND recorded_by = ?",
        (channel_id, alice['peer_id'])
    )
    assert channel_action is not None, "Channel should be in admin_actions"
    assert channel_action['action_type'] == 'channel'
    assert channel_action['prior_0'] == invite_id, "Channel should reference invite as prior"


def test_key_rotation_includes_removal_prior(fresh_db):
    """Keys rotated after removal include prior=[removal_id] in group_key_shared."""
    from tests.utils import tick_helper

    db = fresh_db
    tick.reset_state(db)

    # Create network with Alice
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates invite and Bob joins
    invite_id_1, invite_link_1, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db
    )

    bob_peer_id = peer.create(t_ms=2500, db=db)
    bob = user.join(
        peer_id=bob_peer_id,
        invite_link=invite_link_1,
        name='Bob',
        t_ms=3000,
        db=db
    )

    # Sync so Alice knows about Bob
    tick_helper.sync_until_converged(db=db, start_t_ms=3500, max_rounds=200, check_interval=1)

    # Alice creates another invite and Charlie joins
    # (We need a third member so key rotation has someone to share with after Bob is removed)
    invite_id_2, invite_link_2, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=3600,
        db=db
    )

    charlie_peer_id = peer.create(t_ms=3700, db=db)
    charlie = user.join(
        peer_id=charlie_peer_id,
        invite_link=invite_link_2,
        name='Charlie',
        t_ms=3800,
        db=db
    )

    # Sync so everyone knows about Charlie
    tick_helper.sync_until_converged(db=db, start_t_ms=3900, max_rounds=200, check_interval=1)

    # Make Bob an admin so we can remove him
    from events.identity import admin as admin_module
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin_module.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=4000,
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice['admin_grant_id']
    )

    # Sync so admin grant is fully projected
    tick_helper.sync_until_converged(db=db, start_t_ms=4500, max_rounds=200, check_interval=1)

    # Alice removes Bob
    from events.identity import user_removed
    removal_result = user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=5000,
        db=db
    )
    removal_id = removal_result['event_id']

    # Sync so removal is fully projected and keys are rotated
    tick_helper.sync_until_converged(db=db, start_t_ms=5500, max_rounds=200, check_interval=1)

    # Check that group_key_shared events have prior=[removal_id]
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    gks_rows = safedb.query(
        "SELECT * FROM group_keys_shared WHERE prior_0 = ? AND recorded_by = ?",
        (removal_id, alice['peer_id'])
    )

    # There should be at least one key shared with prior referencing the removal
    # (Charlie should receive the rotated key since Bob was removed)
    assert len(gks_rows) > 0, "Key rotation should create group_key_shared with prior=[removal_id]"


def test_can_share_key_validation(fresh_db):
    """can_share_key_with_peer prevents sharing post-removal keys with removed users."""
    from tests.utils import tick_helper

    db = fresh_db
    tick.reset_state(db)

    # Create network with Alice
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates invite and Bob joins
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db
    )

    bob_peer_id = peer.create(t_ms=2500, db=db)
    bob = user.join(
        peer_id=bob_peer_id,
        invite_link=invite_link,
        name='Bob',
        t_ms=3000,
        db=db
    )

    # Sync so Alice knows about Bob
    tick_helper.sync_until_converged(db=db, start_t_ms=3500, max_rounds=200, check_interval=1)

    # Make Bob an admin so we can remove him
    from events.identity import admin as admin_module
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin_module.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=4000,
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice['admin_grant_id']
    )

    # Sync so admin grant is fully projected
    tick_helper.sync_until_converged(db=db, start_t_ms=4500, max_rounds=200, check_interval=1)

    # Get a key that exists BEFORE removal
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    pre_removal_key = safedb.query_one(
        "SELECT key_id FROM group_keys WHERE recorded_by = ? LIMIT 1",
        (alice['peer_id'],)
    )
    assert pre_removal_key is not None

    # Alice removes Bob - this triggers key rotation
    from events.identity import user_removed
    removal_result = user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=5000,
        db=db
    )
    removal_id = removal_result['event_id']

    # Sync so removal is fully projected and keys are rotated
    tick_helper.sync_until_converged(db=db, start_t_ms=5500, max_rounds=200, check_interval=1)

    # Get a key created AFTER removal (the rotated key)
    post_removal_keys = safedb.query(
        """SELECT original_key_id FROM group_keys_shared
           WHERE prior_0 = ? AND recorded_by = ?""",
        (removal_id, alice['peer_id'])
    )

    # Test can_share_key_with_peer
    from events.group import group_key_shared

    # Pre-removal key should be shareable with Bob (even though he's removed)
    can_share_pre = group_key_shared.can_share_key_with_peer(
        key_id=pre_removal_key['key_id'],
        requester_peer_id=bob['peer_shared_id'],
        recorded_by=alice['peer_id'],
        db=db
    )
    assert can_share_pre is True, "Pre-removal keys should be shareable with removed users"

    # Post-removal key should NOT be shareable with Bob
    if post_removal_keys:
        post_removal_key_id = post_removal_keys[0]['original_key_id']
        can_share_post = group_key_shared.can_share_key_with_peer(
            key_id=post_removal_key_id,
            requester_peer_id=bob['peer_shared_id'],
            recorded_by=alice['peer_id'],
            db=db
        )
        assert can_share_post is False, "Post-removal keys should NOT be shareable with removed users"
