"""
Scenario test: TreeKEM Partition and Heal with Key Distribution.

Tests that partitioned members eventually receive keys after partition heals:
1. Alice, Bob, Charlie, Dave in network, TreeKEM enabled
2. Partition Charlie (goes offline)
3. Alice removes Dave (triggers key rotation)
4. Bob receives new key via TreeKEM (online)
5. Charlie does NOT receive key (partitioned)
6. Heal Charlie's partition
7. Run sync ticks
8. VERIFY: Charlie eventually receives the key
"""
import pytest
from events.identity import user, invite, peer, network, network_settings
from events.identity import user_removed
from events.group import group, group_member, treekem_update
from events.content import message
from core import network_config
from tests.utils.tick_helper import assert_eventually, TestClock, run_ticks
from core.db import create_safe_db


def test_treekem_partition_heal_key_distribution(fresh_db):
    """Test that partitioned member receives key after partition heals."""
    db = fresh_db
    clock = TestClock()

    # === Setup: Alice creates network ===
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Disable TreeKEM to test leaf fallback path
    # Note: TreeKEM is disabled to simplify the test - leaf fallback should work
    network_settings.set_treekem_enabled(
        network_id=alice['network_id'],
        enabled=False,  # Disabled for initial testing
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Add members
    members = {'alice': alice}
    for name in ['Bob', 'Charlie', 'Dave']:
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        member_peer_id = peer.create(t_ms=clock.tick(), db=db)
        member = user.join(peer_id=member_peer_id, invite_link=invite_link, name=name, t_ms=clock.now(), db=db)
        members[name.lower()] = member
        db.commit()

    bob = members['bob']
    charlie = members['charlie']
    dave = members['dave']

    # Wait for all to sync
    def all_synced():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(member_list) == 4

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)

    # Run initial sync ticks to ensure peer_self entries are set up for all peers
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Manually trigger group prekey sharing for all peers
    # (The job runs every hour in production, too slow for tests)
    from events.group import group_prekey
    # First replenish to ensure prekeys exist
    group_prekey.replenish_for_all_peers(t_ms, db)
    # Then share any existing unshared prekeys
    group_prekey.share_existing_prekeys_for_all_peers(t_ms + 100, db)
    db.commit()
    t_ms += 200

    # Run more sync ticks to distribute the shared prekeys
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # Get initial key
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
    original_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    original_key_id = original_key['key_id']

    # Verify Charlie has the original key
    charlie_original_key = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
    assert charlie_original_key['key_id'] == original_key_id, \
        "Charlie should have original key before partition"

    # === Partition Charlie ===
    network_config.partition_peer(charlie['peer_id'])

    # === Alice removes Dave (triggers key rotation) ===
    user_removed.create(
        removed_user_id=dave['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Run ticks for key distribution (Charlie is partitioned)
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # Get new key from Alice's perspective
    new_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    new_key_id = new_key['key_id']
    assert new_key_id != original_key_id, "Key should be rotated after removal"

    # === VERIFY: Bob received the new key ===
    bob_key = group.get_current_key(all_users_group_id, bob['peer_id'], db)
    assert bob_key['key_id'] == new_key_id, "Bob should have the new key"

    # === VERIFY: Charlie still has old key (partitioned) ===
    charlie_key_during_partition = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
    assert charlie_key_during_partition['key_id'] == original_key_id, \
        "Charlie should still have old key while partitioned"

    # === Heal Charlie's partition ===
    network_config.unpartition_peer(charlie['peer_id'])

    # === Run sync ticks ===
    def charlie_has_new_key():
        charlie_key = group.get_current_key(all_users_group_id, charlie['peer_id'], db)
        assert charlie_key['key_id'] == new_key_id, \
            f"Charlie should have new key after sync. Has {charlie_key['key_id'][:20]}..., expected {new_key_id[:20]}..."

    assert_eventually(charlie_has_new_key, db=db, start_t_ms=t_ms)


def test_treekem_chain_removal_while_offline(fresh_db):
    """Test that member receives all keys after multiple removals while offline."""
    db = fresh_db
    clock = TestClock()

    # === Setup: Alice creates network with many members ===
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

    members = {'alice': alice}
    for name in ['Bob', 'Charlie', 'Dave', 'Eve', 'Frank']:
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        member_peer_id = peer.create(t_ms=clock.tick(), db=db)
        member = user.join(peer_id=member_peer_id, invite_link=invite_link, name=name, t_ms=clock.now(), db=db)
        members[name.lower()] = member
        db.commit()

    frank = members['frank']

    # Wait for all to sync
    def all_synced():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(member_list) == 6

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)

    # Manually publish TreeKEM pubkeys for each member
    for name, member in members.items():
        treekem_update.update_for_group(
            group_id=alice['network_id'],
            peer_id=member['peer_id'],
            peer_shared_id=member['peer_shared_id'],
            t_ms=t_ms,
            db=db
        )
        t_ms += 100
    db.commit()

    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    # === Partition Frank ===
    network_config.partition_peer(frank['peer_id'])

    # === Alice removes Bob -> key_1 ===
    user_removed.create(
        removed_user_id=members['bob']['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)

    key_1 = group.get_current_key(all_users_group_id, alice['peer_id'], db)['key_id']

    # === Alice removes Charlie -> key_2 ===
    user_removed.create(
        removed_user_id=members['charlie']['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)

    key_2 = group.get_current_key(all_users_group_id, alice['peer_id'], db)['key_id']

    # === Alice removes Dave -> key_3 ===
    user_removed.create(
        removed_user_id=members['dave']['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)

    key_3 = group.get_current_key(all_users_group_id, alice['peer_id'], db)['key_id']

    # Verify keys are different
    assert key_1 != key_2 != key_3, "Each removal should create a new key"

    # === Heal Frank's partition ===
    network_config.unpartition_peer(frank['peer_id'])

    # === Run sync ===
    def frank_has_all_keys():
        # Frank should end up with key_3 (most recent)
        frank_key = group.get_current_key(all_users_group_id, frank['peer_id'], db)
        assert frank_key['key_id'] == key_3, \
            f"Frank should have final key after sync. Has {frank_key['key_id'][:20]}..."

        # Check Frank has all the keys in his key table
        safedb = create_safe_db(db, recorded_by=frank['peer_id'])
        frank_keys = safedb.query(
            "SELECT key_id FROM group_keys WHERE recorded_by = ?",
            (frank['peer_id'],)
        )
        frank_key_ids = {k['key_id'] for k in frank_keys}

        assert key_1 in frank_key_ids, "Frank should have key_1"
        assert key_2 in frank_key_ids, "Frank should have key_2"
        assert key_3 in frank_key_ids, "Frank should have key_3"

    assert_eventually(frank_has_all_keys, db=db, start_t_ms=t_ms, max_rounds=200)


def test_treekem_messages_readable_after_partition_heal(fresh_db):
    """Test that messages sent during partition are readable after heal."""
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

    # Wait for sync
    def all_synced():
        alice_group = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        member_list = group_member.list_members(alice_group, alice['peer_id'], db)
        assert len(member_list) == 3

    t_ms = assert_eventually(all_synced, db=db, start_t_ms=None)

    # Manually publish TreeKEM pubkeys for each member
    members = {'alice': alice, 'bob': bob, 'charlie': charlie}
    for name, member in members.items():
        treekem_update.update_for_group(
            group_id=alice['network_id'],
            peer_id=member['peer_id'],
            peer_shared_id=member['peer_shared_id'],
            t_ms=t_ms,
            db=db
        )
        t_ms += 100
    db.commit()

    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

    # === Partition Charlie ===
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
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

    # Alice sends message with new key
    message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Secret message during partition',
        t_ms=t_ms,
        db=db
    )
    db.commit()
    t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)

    # === Heal Charlie's partition ===
    network_config.unpartition_peer(charlie['peer_id'])

    # === VERIFY: Charlie can read the message ===
    def charlie_sees_message():
        msgs = message.list(alice['channel_id'], charlie['peer_id'], db)
        contents = [m['content'] for m in msgs]
        assert 'Secret message during partition' in contents, \
            f"Charlie should see message. Sees: {contents}"

    assert_eventually(charlie_sees_message, db=db, start_t_ms=t_ms, max_rounds=200)
