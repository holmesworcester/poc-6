"""
Regression test: Key rotation should only happen once per removal.

Bug fixed: When user_removed events were projected, EVERY peer would rotate
keys independently, creating duplicate/conflicting keys. The fix ensures
only the peer who created the user_removed event rotates keys - other peers
receive the rotated key via group_key_shared.

This test verifies:
1. After user removal + sync, new joiner has exactly the keys shared by inviter
2. No extra keys are created during projection by non-removing peers
"""
import pytest
from core.db import Database, create_safe_db
from core import schema
from events.identity import user, invite, peer, user_removed
from events.group import group_key
from tests.utils import tick_helper
from core import tick


def test_key_rotation_only_by_remover(fresh_db):
    """Verify that key rotation only happens once, by the peer who initiated removal."""
    db = fresh_db
    tick.reset_state(db)

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Bob joins
    invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
    bob_peer_id = peer.create(t_ms=3000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=5)

    # Alice removes Bob (this should create exactly ONE rotation key)
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=5000,
        db=db
    )
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=6000, max_rounds=200, check_interval=5)

    # Count Alice's keys after removal
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    alice_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
    alice_key_ids = set(k['key_id'] for k in alice_keys)
    print(f"Alice has {len(alice_key_ids)} keys after removal")

    # Charlie joins via new invite
    invite2_id, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=7000, db=db)
    charlie_peer_id = peer.create(t_ms=8000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=8000, db=db)
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=9000, max_rounds=200, check_interval=5)

    # Count Charlie's keys after sync
    safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
    charlie_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (charlie['peer_id'],))
    charlie_key_ids = set(k['key_id'] for k in charlie_keys)
    print(f"Charlie has {len(charlie_key_ids)} keys after sync")

    # THE KEY ASSERTION: Charlie should have exactly Alice's keys, no more
    extra_keys = charlie_key_ids - alice_key_ids
    missing_keys = alice_key_ids - charlie_key_ids

    assert not extra_keys, (
        f"Charlie has {len(extra_keys)} extra keys that Alice didn't share! "
        f"This indicates key rotation happened multiple times during projection. "
        f"Extra keys: {list(extra_keys)}"
    )

    assert not missing_keys, (
        f"Charlie is missing {len(missing_keys)} keys that Alice has. "
        f"Missing keys: {list(missing_keys)}"
    )

    print(f"✓ Charlie has exactly the same {len(charlie_key_ids)} keys as Alice")


def test_multiple_removals_no_key_explosion(fresh_db):
    """Verify that multiple removals don't cause exponential key growth."""
    db = fresh_db
    tick.reset_state(db)

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    t_ms = 2000

    # Add and remove 3 users
    for i in range(3):
        # Create invite and join
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
        t_ms += 100
        user_peer_id = peer.create(t_ms=t_ms, db=db)
        joined = user.join(peer_id=user_peer_id, invite_link=invite_link, name=f'User{i}', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()
        tick_helper.sync_until_converged(db=db, start_t_ms=t_ms, max_rounds=200, check_interval=5)
        t_ms += 500

        # Remove the user
        user_removed.create(
            removed_user_id=joined['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )
        t_ms += 100
        db.commit()
        tick_helper.sync_until_converged(db=db, start_t_ms=t_ms, max_rounds=200, check_interval=5)
        t_ms += 500

    # Count Alice's keys - should be 4 (1 original + 3 rotations)
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    alice_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
    alice_key_count = len(alice_keys)
    print(f"Alice has {alice_key_count} keys after 3 removals")

    # Expected: 1 original + 3 rotations = 4 keys
    # If the bug existed, we'd have many more (each sync would add extra keys)
    expected_max_keys = 4  # 1 original + 3 rotations
    assert alice_key_count <= expected_max_keys, (
        f"Alice has {alice_key_count} keys, expected at most {expected_max_keys}. "
        f"This suggests duplicate key creation during projection."
    )

    print(f"✓ Alice has {alice_key_count} keys after 3 removals (expected max: {expected_max_keys})")
