"""
Scenario test: Split-Brain Removals (SECURITY - HARD CASE).

Tests the security-critical scenario where two admins remove different members
while partitioned from each other:

1. Network with Alice (admin), Carol (admin), Bob, Dave, Eve
2. Enable TreeKEM, let pubkeys publish
3. Partition Alice from Carol (create two partitions)
4. Alice removes Bob -> creates key_A (excludes Bob, NOT Dave)
5. Carol removes Dave -> creates key_C (excludes Dave, NOT Bob)
6. IN PARTITION: Bob can still decrypt key_C, Dave can still decrypt key_A
7. Heal partition
8. Run sync ticks until converged
9. VERIFY SECURITY: After convergence, NEITHER Bob NOR Dave can decrypt current key

KNOWN BUG (HIGH SEVERITY): rotate_for_removal() only excludes the single member
being removed, not all concurrent removals. Security hole during eventual consistency.
"""
import pytest
from events.identity import user, invite, peer, network, network_settings
from events.identity import user_removed, admin
from events.group import group, group_member, group_prekey
from events.content import message
from core import network_config
from tests.utils.tick_helper import assert_eventually, TestClock, run_ticks
from core.db import create_safe_db


def test_split_brain_removals_both_excluded_after_convergence(fresh_db):
    """Test that split-brain removals result in both removed members being locked out.

    This is a SECURITY test - if it fails, removed members may retain access to messages.
    """
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

    # Add members: Carol (will be admin), Bob, Dave, Eve
    members = {'alice': alice}

    for name in ['Carol', 'Bob', 'Dave', 'Eve']:
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        member_peer_id = peer.create(t_ms=clock.tick(), db=db)
        member = user.join(peer_id=member_peer_id, invite_link=invite_link, name=name, t_ms=clock.now(), db=db)
        members[name.lower()] = member
        db.commit()

    carol = members['carol']
    bob = members['bob']
    dave = members['dave']
    eve = members['eve']

    # Wait for all to sync
    def all_synced():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(member_list) == 5

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)

    # Make Carol an admin
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin.create(
        user_id=carol['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=clock.tick(),
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice['admin_grant_id']
    )
    db.commit()

    # Distribute group prekeys
    group_prekey.replenish_for_all_peers(t_ms, db)
    group_prekey.share_existing_prekeys_for_all_peers(t_ms + 100, db)
    db.commit()
    t_ms += 200

    # Let TreeKEM pubkeys and prekeys propagate
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    # Get initial key
    original_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    original_key_id = original_key['key_id']

    # === Create partition: Alice and Carol in separate partitions ===
    # Partition Carol (simulates network split)
    network_config.partition_peer(carol['peer_id'])

    # === Alice removes Bob ===
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Run ticks for Alice's removal to process (Carol is partitioned)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    key_a = group.get_current_key(all_users_group_id, alice['peer_id'], db)['key_id']
    assert key_a != original_key_id, "Key should rotate after Bob's removal"

    # === Carol removes Dave (in partition) ===
    # Unpartition Carol temporarily to let her act
    network_config.unpartition_peer(carol['peer_id'])

    # But partition Alice now (swap the partition)
    network_config.partition_peer(alice['peer_id'])

    # Carol removes Dave
    user_removed.create(
        removed_user_id=dave['user_id'],
        removed_by_peer_id=carol['peer_shared_id'],
        removed_by_local_peer_id=carol['peer_id'],
        t_ms=t_ms + 1,  # Slightly later
        db=db
    )
    db.commit()

    # Run ticks for Carol's removal
    t_ms = run_ticks(db=db, start_t_ms=t_ms + 1, num_rounds=30)

    key_c = group.get_current_key(all_users_group_id, carol['peer_id'], db)['key_id']

    # === Heal all partitions ===
    network_config.unpartition_peer(alice['peer_id'])
    network_config.unpartition_peer(carol['peer_id'])

    # === Run sync to convergence ===
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=200)

    # === VERIFY: Both Bob and Dave should be removed from member list ===
    def both_removed_from_member_list():
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        member_names = [m['name'] for m in member_list]
        assert 'Bob' not in member_names, "Bob should be removed"
        assert 'Dave' not in member_names, "Dave should be removed"
        assert 'Alice' in member_names
        assert 'Carol' in member_names
        assert 'Eve' in member_names
        assert len(member_list) == 3, f"Should have 3 members, got {len(member_list)}"

    assert_eventually(both_removed_from_member_list, db=db, start_t_ms=t_ms)

    # === VERIFY SECURITY: Current key should exclude both Bob AND Dave ===
    final_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)

    # Check Bob's keys
    safedb_bob = create_safe_db(db, recorded_by=bob['peer_id'])
    bob_keys = safedb_bob.query(
        "SELECT key_id FROM group_keys WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    bob_key_ids = {k['key_id'] for k in bob_keys}

    # Check Dave's keys
    safedb_dave = create_safe_db(db, recorded_by=dave['peer_id'])
    dave_keys = safedb_dave.query(
        "SELECT key_id FROM group_keys WHERE recorded_by = ?",
        (dave['peer_id'],)
    )
    dave_key_ids = {k['key_id'] for k in dave_keys}

    # The critical security assertion:
    # Neither Bob nor Dave should have the FINAL current key
    # They may have intermediate keys (key_a or key_c) but not the final one

    # Note: This test documents expected behavior. If the system doesn't
    # re-rotate after detecting concurrent removals, this will fail.
    assert final_key['key_id'] not in bob_key_ids, \
        "SECURITY: Bob should NOT have access to the final key after being removed"

    assert final_key['key_id'] not in dave_key_ids, \
        "SECURITY: Dave should NOT have access to the final key after being removed"

    # === VERIFY: Eve (not removed) should have the current key ===
    def eve_has_final_key():
        eve_key = group.get_current_key(all_users_group_id, eve['peer_id'], db)
        assert eve_key is not None, "Eve should have a key"
        assert eve_key['key_id'] == final_key['key_id'], \
            f"Eve should have final key. Has {eve_key['key_id'][:20]}..., expected {final_key['key_id'][:20]}..."

    assert_eventually(eve_has_final_key, db=db, start_t_ms=t_ms)


def test_split_brain_messages_after_removal_not_readable_by_removed(fresh_db):
    """Test that messages sent after split-brain removals are not readable by removed members."""
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

    # Add Bob, Carol, Dave
    members = {'alice': alice}
    for name in ['Bob', 'Carol', 'Dave']:
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        member_peer_id = peer.create(t_ms=clock.tick(), db=db)
        member = user.join(peer_id=member_peer_id, invite_link=invite_link, name=name, t_ms=clock.now(), db=db)
        members[name.lower()] = member
        db.commit()

    bob = members['bob']
    carol = members['carol']
    dave = members['dave']

    # Make Carol admin
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin.create(
        user_id=carol['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=clock.tick(),
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice['admin_grant_id']
    )
    db.commit()

    # Sync
    def all_synced():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(member_list) == 4

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Distribute group prekeys
    group_prekey.replenish_for_all_peers(t_ms, db)
    group_prekey.share_existing_prekeys_for_all_peers(t_ms + 100, db)
    db.commit()
    t_ms += 200
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # Send message before any removals (everyone can read)
    message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Message before any removal',
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Bob should see the pre-removal message
    def bob_sees_pre_removal():
        msgs = message.list(alice['channel_id'], bob['peer_id'], db)
        contents = [m['content'] for m in msgs]
        assert 'Message before any removal' in contents

    assert_eventually(bob_sees_pre_removal, db=db, start_t_ms=t_ms)

    # === Alice removes Bob ===
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # Send message after Bob's removal
    message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Secret message after Bob removed',
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=100)

    # Carol and Dave should see the post-removal message
    def carol_sees_message():
        msgs = message.list(alice['channel_id'], carol['peer_id'], db)
        contents = [m['content'] for m in msgs]
        assert 'Secret message after Bob removed' in contents

    assert_eventually(carol_sees_message, db=db, start_t_ms=t_ms)

    def dave_sees_message():
        msgs = message.list(alice['channel_id'], dave['peer_id'], db)
        contents = [m['content'] for m in msgs]
        assert 'Secret message after Bob removed' in contents

    assert_eventually(dave_sees_message, db=db, start_t_ms=t_ms)

    # Bob should NOT see the post-removal message
    # (He doesn't have the new key)
    bob_msgs = message.list(alice['channel_id'], bob['peer_id'], db)
    bob_contents = [m['content'] for m in bob_msgs]
    assert 'Secret message after Bob removed' not in bob_contents, \
        "SECURITY: Bob should NOT see message sent after his removal"


def test_split_brain_needs_rerotation_detection(fresh_db):
    """Test that system detects and handles concurrent removals.

    Expected behavior after fix:
    1. After partition heals, system detects both removal events
    2. System realizes current key was created before knowing about other removal
    3. System triggers a re-rotation that excludes ALL removed members

    This test documents the expected fix behavior.
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

    # Add Carol (admin), Bob, Dave
    members = {'alice': alice}
    for name in ['Carol', 'Bob', 'Dave']:
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        member_peer_id = peer.create(t_ms=clock.tick(), db=db)
        member = user.join(peer_id=member_peer_id, invite_link=invite_link, name=name, t_ms=clock.now(), db=db)
        members[name.lower()] = member
        db.commit()

    carol = members['carol']

    # Make Carol admin
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin.create(
        user_id=carol['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=clock.tick(),
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice['admin_grant_id']
    )
    db.commit()

    # Sync
    def all_synced():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(member_list) == 4

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Distribute group prekeys
    group_prekey.replenish_for_all_peers(t_ms, db)
    group_prekey.share_existing_prekeys_for_all_peers(t_ms + 100, db)
    db.commit()
    t_ms += 200
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    # Partition Carol
    network_config.partition_peer(carol['peer_id'])

    # Alice removes Bob
    user_removed.create(
        removed_user_id=members['bob']['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)

    # Swap partitions
    network_config.unpartition_peer(carol['peer_id'])
    network_config.partition_peer(alice['peer_id'])

    # Carol removes Dave
    user_removed.create(
        removed_user_id=members['dave']['user_id'],
        removed_by_peer_id=carol['peer_shared_id'],
        removed_by_local_peer_id=carol['peer_id'],
        t_ms=t_ms + 1,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms + 1, num_rounds=20)

    # Heal all
    network_config.unpartition_peer(alice['peer_id'])

    # Run sync to convergence
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=200)

    # Verify security property: the final key should not be accessible
    # to either removed user
    final_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    assert final_key is not None, "Alice should have a final key"

    # Get keys known to Bob and Dave
    safedb_bob = create_safe_db(db, recorded_by=members['bob']['peer_id'])
    bob_keys = safedb_bob.query(
        "SELECT key_id FROM group_keys WHERE recorded_by = ?",
        (members['bob']['peer_id'],)
    )
    bob_key_ids = {k['key_id'] for k in bob_keys}

    safedb_dave = create_safe_db(db, recorded_by=members['dave']['peer_id'])
    dave_keys = safedb_dave.query(
        "SELECT key_id FROM group_keys WHERE recorded_by = ?",
        (members['dave']['peer_id'],)
    )
    dave_key_ids = {k['key_id'] for k in dave_keys}

    assert final_key['key_id'] not in bob_key_ids, \
        "SECURITY: Bob should NOT have access to the final key after being removed"

    assert final_key['key_id'] not in dave_key_ids, \
        "SECURITY: Dave should NOT have access to the final key after being removed"
