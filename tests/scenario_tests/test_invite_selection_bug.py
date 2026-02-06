"""
Scenario test: Verify correct invite selection for device link key seeding.

This test exposes a bug where peer_shared.project() uses a time-based heuristic
(ORDER BY created_at DESC LIMIT 1) to select which invite to use for seeding
group keys to a newly linked device, instead of using the specific invite_id
from the peer_shared event itself.

The bug manifests when:
1. Multiple mode='peer' invites exist for the same user
2. A group is created AFTER the invites (so keys need to be seeded during peer_shared.project)
3. A device joins using an OLDER invite
4. The time-based heuristic incorrectly selects the NEWER invite for key seeding
5. Keys are sealed to the wrong invite's prekey
6. The new device cannot decrypt the keys

Critical insight: Keys sealed during invite.create() work fine because they use
the specific invite. The bug is in peer_shared.project() which seeds keys for
groups created AFTER the invite was made - it uses a time-based heuristic to
select which invite to use instead of reading it from the peer_shared event.

Test scenario:
1. Alice creates network on device A
2. Alice creates device link invite_1 (OLDER)
3. Alice creates device link invite_2 (NEWER)
4. Alice creates a group AFTER the invites (keys must be seeded during peer_shared.project)
5. Device B joins using invite_1 (the OLDER invite)
6. Device A receives device B's peer_shared
7. Device A's peer_shared.project() tries to seed keys
8. BUG: It selects invite_2 (newest) instead of invite_1 (from peer_shared event)
9. Keys are sealed to invite_2's prekey
10. Device B cannot decrypt (has invite_1's private key, not invite_2's)

Expected behavior: Keys should be sealed to invite_1's prekey (the invite
that device B actually used), which is available in the peer_shared event data.
"""
import pytest
from core.db import create_safe_db
from events.identity import user, invite, peer_shared, peer
from events.group import group, group_member
from tests.utils.tick_helper import assert_eventually


def test_device_joins_with_older_invite_gets_correct_keys(fresh_db):
    """Device joining with older invite should get keys sealed to that invite's prekey.

    This test creates the group AFTER the invites to ensure key seeding happens
    in peer_shared.project() rather than invite.create().
    """
    db = fresh_db

    # Alice creates network
    alice_device_a = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Wait for network setup
    def network_setup_complete():
        channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (alice_device_a['peer_id'],)
        )
        assert len(channels) >= 1

    t_ms = assert_eventually(network_setup_complete, db=db, start_t_ms=None)

    # Alice creates FIRST device link invite (OLDER)
    invite_1_id, invite_1_link, invite_1_data = invite.create(
        peer_id=alice_device_a['peer_id'],
        t_ms=t_ms,
        db=db,
        mode='peer',
        user_id=alice_device_a['user_id']
    )
    db.commit()

    # Alice creates SECOND device link invite (NEWER)
    invite_2_id, invite_2_link, invite_2_data = invite.create(
        peer_id=alice_device_a['peer_id'],
        t_ms=t_ms + 1000,
        db=db,
        mode='peer',
        user_id=alice_device_a['user_id']
    )
    db.commit()

    # NOW create the group AFTER the invites
    private_group_id, private_group_key_id = group.create(
        name='Private Group',
        peer_id=alice_device_a['peer_id'],
        peer_shared_id=alice_device_a['peer_shared_id'],
        t_ms=t_ms + 2000,
        db=db
    )

    # Add Alice as member
    group_member.create(
        group_id=private_group_id,
        user_id=alice_device_a['user_id'],
        peer_id=alice_device_a['peer_id'],
        peer_shared_id=alice_device_a['peer_shared_id'],
        t_ms=t_ms + 2001,
        db=db
    )
    db.commit()

    # Wait for group setup
    def group_setup_complete():
        groups = db.query_all(
            "SELECT group_id FROM groups WHERE recorded_by = ?",
            (alice_device_a['peer_id'],)
        )
        assert len(groups) >= 2  # all_users + private group

    t_ms = assert_eventually(group_setup_complete, db=db, start_t_ms=t_ms + 2000)

    # Verify we have two different invites
    assert invite_1_id != invite_2_id, "Should have two different invite IDs"
    assert invite_1_data['invite_prekey_id'] != invite_2_data['invite_prekey_id'], \
        "Should have two different invite prekey IDs"

    # Query invites table to confirm both exist
    safedb = create_safe_db(db, recorded_by=alice_device_a['peer_id'])
    invite_rows = safedb.query(
        "SELECT invite_id, created_at FROM invites WHERE mode = 'peer' AND user_id = ? AND recorded_by = ? ORDER BY created_at DESC",
        (alice_device_a['user_id'], alice_device_a['peer_id'])
    )
    assert len(invite_rows) >= 2, "Should have at least 2 peer invites"
    assert invite_rows[0]['invite_id'] == invite_2_id, "Newest invite should be invite_2"
    assert invite_rows[1]['invite_id'] == invite_1_id, "Older invite should be invite_1"

    # Device B joins using invite_1 (the OLDER invite)
    device_b_peer_id = peer.create(t_ms=t_ms + 3000, db=db)
    accepted = invite.accept(device_b_peer_id, invite_1_link, t_ms=t_ms + 3001, db=db)

    # Verify we're using the older invite
    assert accepted['invite_id'] == invite_1_id, "Device B should be using invite_1"

    alice_device_b = peer_shared.join(
        peer_id=device_b_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=t_ms + 3002,
        db=db,
        device_name='Device B'
    )
    db.commit()

    # Verify device B has same user_id
    assert alice_device_b['user_id'] == alice_device_a['user_id'], \
        "Both devices should have same user_id"

    # Wait for key propagation
    def device_b_has_key():
        has_key = db.query_one(
            "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
            (private_group_key_id, alice_device_b['peer_id'])
        )
        assert has_key is not None, (
            f"Device B should have the private group key!\n"
            f"This fails if peer_shared.project() uses time-based invite selection."
        )

    assert_eventually(device_b_has_key, db=db, start_t_ms=t_ms + 3000)


def test_multiple_invites_keys_sealed_to_correct_prekey(fresh_db):
    """Verify which invite's prekey the keys are actually sealed to."""
    db = fresh_db

    alice_device_a = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create a group
    private_group_id, private_group_key_id = group.create(
        name='Private Group',
        peer_id=alice_device_a['peer_id'],
        peer_shared_id=alice_device_a['peer_shared_id'],
        t_ms=2000,
        db=db
    )
    group_member.create(
        group_id=private_group_id,
        user_id=alice_device_a['user_id'],
        peer_id=alice_device_a['peer_id'],
        peer_shared_id=alice_device_a['peer_shared_id'],
        t_ms=2001,
        db=db
    )
    db.commit()

    # Wait for group creation
    def group_created():
        groups = db.query_all(
            "SELECT group_id FROM groups WHERE recorded_by = ?",
            (alice_device_a['peer_id'],)
        )
        assert len(groups) >= 2

    t_ms = assert_eventually(group_created, db=db, start_t_ms=None)

    # Create TWO invites
    invite_1_id, invite_1_link, invite_1_data = invite.create(
        peer_id=alice_device_a['peer_id'],
        t_ms=t_ms,
        db=db,
        mode='peer',
        user_id=alice_device_a['user_id']
    )
    db.commit()

    invite_2_id, invite_2_link, invite_2_data = invite.create(
        peer_id=alice_device_a['peer_id'],
        t_ms=t_ms + 1000,
        db=db,
        mode='peer',
        user_id=alice_device_a['user_id']
    )
    db.commit()

    # Device B joins using invite_1 (OLDER)
    device_b_peer_id = peer.create(t_ms=t_ms + 2000, db=db)
    accepted = invite.accept(device_b_peer_id, invite_1_link, t_ms=t_ms + 2001, db=db)

    alice_device_b = peer_shared.join(
        peer_id=device_b_peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=t_ms + 2002,
        db=db,
        device_name='Device B'
    )
    db.commit()

    # Verify device B has invite_1's prekey
    device_b_prekeys = db.query(
        "SELECT prekey_id FROM group_prekeys WHERE recorded_by = ?",
        (alice_device_b['peer_id'],)
    )
    has_invite_1_prekey = any(
        pk['prekey_id'] == invite_1_data['invite_prekey_id']
        for pk in device_b_prekeys
    )
    assert has_invite_1_prekey, "Device B should have invite_1's prekey"

    # Wait for key propagation
    def device_b_has_key():
        device_b_keys = db.query(
            "SELECT key_id FROM group_keys WHERE recorded_by = ?",
            (alice_device_b['peer_id'],)
        )
        has_private_key = any(
            k['key_id'] == private_group_key_id
            for k in device_b_keys
        )
        assert has_private_key, (
            "Device B should have the private group key. "
            "If this fails, keys were likely sealed to the wrong invite's prekey."
        )

    assert_eventually(device_b_has_key, db=db, start_t_ms=t_ms + 2000)
