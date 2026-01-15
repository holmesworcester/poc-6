"""
Scenario test: New user joining after another user was banned.

Reproduces bug where Bobby can't see messages after joining a network
where Bob was previously banned. The error is:
"Network-signed all_users group not found for network"

Test scenario:
1. Holmes creates a network and sends a message
2. Bob joins via invite and sends a message
3. Holmes bans Bob (user_removed)
4. Holmes sends another message (with image)
5. Bobby joins via a new invite
6. Bobby should see all messages and the user list

The bug: Bobby's peer doesn't have the all_users group projected yet,
because the group event hasn't synced/projected from Bobby's perspective.
"""
import pytest
from core.db import Database, create_safe_db
from core import schema
from events.identity import user, invite, peer, user_removed, network
from events.content import message
from events.group import group_member
from tests.utils import tick_helper


def test_new_user_joins_after_ban(fresh_db):
    """Test that a new user can join and see messages after another user was banned."""
    db = fresh_db

    print("\n=== Setup: Holmes creates network ===")
    holmes = user.new_network(name='Holmes', t_ms=1000, db=db)
    print(f"Holmes created network: {holmes['network_id'][:20]}...")

    # Holmes sends first message
    print("\n=== Holmes sends first message ===")
    holmes_msg1 = message.create(
        peer_id=holmes['peer_id'],
        channel_id=holmes['channel_id'],
        content='yo',
        t_ms=2000,
        db=db
    )
    print(f"Holmes sent: yo")
    db.commit()

    # Holmes creates invite for Bob
    print("\n=== Holmes creates invite for Bob ===")
    invite1_id, invite1_link, _ = invite.create(
        peer_id=holmes['peer_id'],
        t_ms=3000,
        db=db
    )
    print(f"Created invite #1")

    # Bob joins
    print("\n=== Bob joins network ===")
    bob_peer_id = peer.create(t_ms=4000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=4000, db=db)
    print(f"Bob joined: {bob['user_id'][:20]}...")
    db.commit()

    # Sync to converge
    print("\n=== Initial sync ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=5000, max_rounds=200, check_interval=1)

    # Bob sends a message
    print("\n=== Bob sends message ===")
    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content='hey',
        t_ms=6000,
        db=db
    )
    print(f"Bob sent: hey")
    db.commit()

    # Sync
    tick_helper.sync_until_converged(db=db, start_t_ms=7000, max_rounds=200, check_interval=1)

    # Holmes bans Bob
    print("\n=== Holmes bans Bob ===")
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=holmes['peer_shared_id'],
        removed_by_local_peer_id=holmes['peer_id'],
        t_ms=8000,
        db=db
    )
    print(f"Bob removed from network")
    db.commit()

    # Sync the removal
    tick_helper.sync_until_converged(db=db, start_t_ms=9000, max_rounds=200, check_interval=1)

    # Holmes sends another message
    print("\n=== Holmes sends another message ===")
    holmes_msg2 = message.create(
        peer_id=holmes['peer_id'],
        channel_id=holmes['channel_id'],
        content='this has an image!',
        t_ms=10000,
        db=db
    )
    print(f"Holmes sent: this has an image!")
    db.commit()

    # Sync
    tick_helper.sync_until_converged(db=db, start_t_ms=11000, max_rounds=200, check_interval=1)

    # Holmes creates invite for Bobby
    print("\n=== Holmes creates invite for Bobby ===")
    invite2_id, invite2_link, _ = invite.create(
        peer_id=holmes['peer_id'],
        t_ms=12000,
        db=db
    )
    print(f"Created invite #2")

    # Bobby joins
    print("\n=== Bobby joins network ===")
    bobby_peer_id = peer.create(t_ms=13000, db=db)
    bobby = user.join(peer_id=bobby_peer_id, invite_link=invite2_link, name='Bobby', t_ms=13000, db=db)
    print(f"Bobby joined: {bobby['user_id'][:20]}...")
    db.commit()

    # Sync to allow Bobby to receive events
    print("\n=== Sync after Bobby joins ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=14000, max_rounds=200, check_interval=1)

    # THE BUG: This should NOT raise an error
    print("\n=== Verify Bobby can access all_users group ===")
    try:
        all_users_group_id = network.get_all_users_group_id(bobby['network_id'], bobby['peer_id'], db)
        print(f"✓ Bobby found all_users group: {all_users_group_id[:20]}...")
    except ValueError as e:
        print(f"✗ ERROR: {e}")
        pytest.fail(f"Bobby should be able to find all_users group, but got: {e}")

    # Verify Bobby can list members
    print("\n=== Verify Bobby can list members ===")
    members = group_member.list_members(all_users_group_id, bobby['peer_id'], db)
    member_names = [m['name'] for m in members]
    print(f"Members from Bobby's view: {member_names}")

    assert 'Holmes' in member_names, "Bobby should see Holmes in member list"
    assert 'Bobby' in member_names, "Bobby should see himself in member list"
    assert 'Bob' not in member_names, "Bobby should NOT see banned Bob in member list"

    # Verify Bobby can see messages
    print("\n=== Verify Bobby can see messages ===")
    bobby_messages = message.list(bobby['channel_id'], bobby['peer_id'], db)
    bobby_message_contents = [msg['content'] for msg in bobby_messages]
    print(f"Bobby sees {len(bobby_messages)} messages: {bobby_message_contents}")

    assert 'yo' in bobby_message_contents, "Bobby should see Holmes's first message"
    assert 'hey' in bobby_message_contents, "Bobby should see Bob's message (before ban)"
    assert 'this has an image!' in bobby_message_contents, "Bobby should see Holmes's second message"

    print("\n✅ test_new_user_joins_after_ban passed!")


def test_three_users_join_sequentially(fresh_db):
    """Test corner case: three users joining sequentially."""
    db = fresh_db

    print("\n=== Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Alice creates invites for Bob and Charlie
    invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)

    # Bob joins
    print("\n=== Bob joins ===")
    bob_peer_id = peer.create(t_ms=3000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1)

    # Alice creates invite for Charlie (after Bob has joined)
    invite2_id, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=5000, db=db)

    # Charlie joins
    print("\n=== Charlie joins ===")
    charlie_peer_id = peer.create(t_ms=6000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=6000, db=db)
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=7000, max_rounds=200, check_interval=1)

    # Verify all three can see each other
    print("\n=== Verify member lists ===")
    for name, user_data in [('Alice', alice), ('Bob', bob), ('Charlie', charlie)]:
        all_users_group_id = network.get_all_users_group_id(user_data['network_id'], user_data['peer_id'], db)
        members = group_member.list_members(all_users_group_id, user_data['peer_id'], db)
        member_names = [m['name'] for m in members]
        print(f"{name} sees members: {member_names}")

        assert 'Alice' in member_names, f"{name} should see Alice"
        assert 'Bob' in member_names, f"{name} should see Bob"
        assert 'Charlie' in member_names, f"{name} should see Charlie"

    print("\n✅ test_three_users_join_sequentially passed!")


def test_user_joins_after_two_bans(fresh_db):
    """Test corner case: user joins after two users were banned."""
    db = fresh_db

    print("\n=== Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Bob joins
    invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
    bob_peer_id = peer.create(t_ms=3000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1)

    # Charlie joins
    invite2_id, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=5000, db=db)
    charlie_peer_id = peer.create(t_ms=6000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=6000, db=db)
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=7000, max_rounds=200, check_interval=1)

    # Alice bans Bob
    print("\n=== Alice bans Bob ===")
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=8000,
        db=db
    )
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=9000, max_rounds=200, check_interval=1)

    # Alice bans Charlie
    print("\n=== Alice bans Charlie ===")
    user_removed.create(
        removed_user_id=charlie['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=10000,
        db=db
    )
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=11000, max_rounds=200, check_interval=1)

    # Dave joins after two bans
    print("\n=== Dave joins after two bans ===")
    invite3_id, invite3_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=12000, db=db)
    dave_peer_id = peer.create(t_ms=13000, db=db)
    dave = user.join(peer_id=dave_peer_id, invite_link=invite3_link, name='Dave', t_ms=13000, db=db)
    db.commit()
    tick_helper.sync_until_converged(db=db, start_t_ms=14000, max_rounds=200, check_interval=1)

    # Verify Dave can access all_users group
    print("\n=== Verify Dave's view ===")
    try:
        all_users_group_id = network.get_all_users_group_id(dave['network_id'], dave['peer_id'], db)
        members = group_member.list_members(all_users_group_id, dave['peer_id'], db)
        member_names = [m['name'] for m in members]
        print(f"Dave sees members: {member_names}")

        assert 'Alice' in member_names, "Dave should see Alice"
        assert 'Dave' in member_names, "Dave should see himself"
        assert 'Bob' not in member_names, "Dave should NOT see banned Bob"
        assert 'Charlie' not in member_names, "Dave should NOT see banned Charlie"
    except ValueError as e:
        pytest.fail(f"Dave should be able to access all_users group, but got: {e}")

    print("\n✅ test_user_joins_after_two_bans passed!")


def test_rapid_join_ban_join_cycle(fresh_db):
    """Test corner case: rapid cycle of join, ban, join."""
    db = fresh_db

    print("\n=== Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    t_ms = 2000

    # Multiple cycles of invite->join->ban
    for i in range(3):
        print(f"\n=== Cycle {i+1}: User{i} joins and gets banned ===")

        # Create invite
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
        t_ms += 100

        # User joins
        user_peer_id = peer.create(t_ms=t_ms, db=db)
        joined_user = user.join(peer_id=user_peer_id, invite_link=invite_link, name=f'User{i}', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()

        # Sync
        _, _, _, _ = tick_helper.sync_until_converged(db=db, start_t_ms=t_ms, max_rounds=200, check_interval=1)
        t_ms += 1000

        # Ban the user
        user_removed.create(
            removed_user_id=joined_user['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )
        t_ms += 100
        db.commit()

        # Sync
        _, _, _, _ = tick_helper.sync_until_converged(db=db, start_t_ms=t_ms, max_rounds=200, check_interval=1)
        t_ms += 1000

    # Now final user joins
    print("\n=== Final user joins after all bans ===")
    _, final_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
    t_ms += 100

    final_peer_id = peer.create(t_ms=t_ms, db=db)
    final_user = user.join(peer_id=final_peer_id, invite_link=final_invite_link, name='FinalUser', t_ms=t_ms, db=db)
    t_ms += 100
    db.commit()

    tick_helper.sync_until_converged(db=db, start_t_ms=t_ms, max_rounds=200, check_interval=1)

    # Verify final user can access everything
    print("\n=== Verify FinalUser's view ===")
    try:
        all_users_group_id = network.get_all_users_group_id(final_user['network_id'], final_user['peer_id'], db)
        members = group_member.list_members(all_users_group_id, final_user['peer_id'], db)
        member_names = [m['name'] for m in members]
        print(f"FinalUser sees members: {member_names}")

        assert 'Alice' in member_names, "FinalUser should see Alice"
        assert 'FinalUser' in member_names, "FinalUser should see himself"
        # All previously banned users should not appear
        for i in range(3):
            assert f'User{i}' not in member_names, f"FinalUser should NOT see banned User{i}"
    except ValueError as e:
        pytest.fail(f"FinalUser should be able to access all_users group, but got: {e}")

    print("\n✅ test_rapid_join_ban_join_cycle passed!")
