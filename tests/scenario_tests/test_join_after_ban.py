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
from core.db import create_safe_db
from events.identity import user, invite, peer, user_removed, network
from events.content import message
from events.group import group_member
from tests.utils.tick_helper import assert_eventually


def test_new_user_joins_after_ban(fresh_db):
    """Test that a new user can join and see messages after another user was banned."""
    db = fresh_db

    holmes = user.new_network(name='Holmes', t_ms=1000, db=db)

    # Holmes sends first message
    holmes_msg1 = message.create(
        peer_id=holmes['peer_id'],
        channel_id=holmes['channel_id'],
        content='yo',
        t_ms=2000,
        db=db
    )
    db.commit()

    # Holmes creates invite for Bob
    _, invite1_link, _ = invite.create(peer_id=holmes['peer_id'], t_ms=3000, db=db)

    # Bob joins
    bob_peer_id = peer.create(t_ms=4000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=4000, db=db)
    db.commit()

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=5000)

    # Bob sends a message
    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content='hey',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for Holmes to see Bob's message
    def holmes_sees_bob_msg():
        holmes_msgs = message.list(holmes['channel_id'], holmes['peer_id'], db)
        assert any(m['content'] == 'hey' for m in holmes_msgs)

    t_ms = assert_eventually(holmes_sees_bob_msg, db=db, start_t_ms=t_ms)

    # Holmes bans Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=holmes['peer_shared_id'],
        removed_by_local_peer_id=holmes['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for ban to take effect
    def bob_is_banned():
        all_users_group_id = network.get_all_users_group_id(holmes['network_id'], holmes['peer_id'], db)
        members = group_member.list_members(all_users_group_id, holmes['peer_id'], db)
        member_names = [m['name'] for m in members]
        assert 'Bob' not in member_names

    t_ms = assert_eventually(bob_is_banned, db=db, start_t_ms=t_ms)

    # Holmes sends another message
    holmes_msg2 = message.create(
        peer_id=holmes['peer_id'],
        channel_id=holmes['channel_id'],
        content='this has an image!',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Holmes creates invite for Bobby
    _, invite2_link, _ = invite.create(peer_id=holmes['peer_id'], t_ms=t_ms + 1000, db=db)

    # Bobby joins
    bobby_peer_id = peer.create(t_ms=t_ms + 2000, db=db)
    bobby = user.join(peer_id=bobby_peer_id, invite_link=invite2_link, name='Bobby', t_ms=t_ms + 2000, db=db)
    db.commit()

    # Wait for Bobby to see everything
    def bobby_sees_all():
        # Bobby can access all_users group
        all_users_group_id = network.get_all_users_group_id(bobby['network_id'], bobby['peer_id'], db)
        members = group_member.list_members(all_users_group_id, bobby['peer_id'], db)
        member_names = [m['name'] for m in members]

        assert 'Holmes' in member_names, "Bobby should see Holmes"
        assert 'Bobby' in member_names, "Bobby should see himself"
        assert 'Bob' not in member_names, "Bobby should NOT see banned Bob"

        # Bobby can see messages
        bobby_messages = message.list(bobby['channel_id'], bobby['peer_id'], db)
        bobby_message_contents = [msg['content'] for msg in bobby_messages]

        assert 'yo' in bobby_message_contents, "Bobby should see Holmes's first message"
        assert 'hey' in bobby_message_contents, "Bobby should see Bob's message (before ban)"
        assert 'this has an image!' in bobby_message_contents, "Bobby should see Holmes's second message"

    assert_eventually(bobby_sees_all, db=db, start_t_ms=t_ms + 2000)


def test_three_users_join_sequentially(fresh_db):
    """Test corner case: three users joining sequentially."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Alice creates invite for Bob
    _, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)

    # Bob joins
    bob_peer_id = peer.create(t_ms=3000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
    db.commit()

    # Wait for Bob to join
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=4000)

    # Alice creates invite for Charlie (after Bob has joined)
    _, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)

    # Charlie joins
    charlie_peer_id = peer.create(t_ms=t_ms + 1000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=t_ms + 1000, db=db)
    db.commit()

    # Wait for all three to see each other
    def all_see_each_other():
        for name, user_data in [('Alice', alice), ('Bob', bob), ('Charlie', charlie)]:
            all_users_group_id = network.get_all_users_group_id(user_data['network_id'], user_data['peer_id'], db)
            members = group_member.list_members(all_users_group_id, user_data['peer_id'], db)
            member_names = [m['name'] for m in members]

            assert 'Alice' in member_names, f"{name} should see Alice"
            assert 'Bob' in member_names, f"{name} should see Bob"
            assert 'Charlie' in member_names, f"{name} should see Charlie"

    assert_eventually(all_see_each_other, db=db, start_t_ms=t_ms + 1000)


def test_user_joins_after_two_bans(fresh_db):
    """Test corner case: user joins after two users were banned."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Bob joins
    _, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
    bob_peer_id = peer.create(t_ms=3000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
    db.commit()

    # Wait for Bob
    def bob_joined():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_joined, db=db, start_t_ms=4000)

    # Charlie joins
    _, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
    charlie_peer_id = peer.create(t_ms=t_ms + 1000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=t_ms + 1000, db=db)
    db.commit()

    # Wait for Charlie
    def charlie_joined():
        charlie_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (charlie['peer_id'],)
        )
        assert len(charlie_channels) >= 1

    t_ms = assert_eventually(charlie_joined, db=db, start_t_ms=t_ms + 1000)

    # Alice bans Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for Bob ban
    def bob_banned():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert 'Bob' not in [m['name'] for m in members]

    t_ms = assert_eventually(bob_banned, db=db, start_t_ms=t_ms)

    # Alice bans Charlie
    user_removed.create(
        removed_user_id=charlie['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for Charlie ban
    def charlie_banned():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert 'Charlie' not in [m['name'] for m in members]

    t_ms = assert_eventually(charlie_banned, db=db, start_t_ms=t_ms)

    # Dave joins after two bans
    _, invite3_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
    dave_peer_id = peer.create(t_ms=t_ms + 1000, db=db)
    dave = user.join(peer_id=dave_peer_id, invite_link=invite3_link, name='Dave', t_ms=t_ms + 1000, db=db)
    db.commit()

    # Wait for Dave to see correct state
    def dave_sees_correct_state():
        all_users_group_id = network.get_all_users_group_id(dave['network_id'], dave['peer_id'], db)
        members = group_member.list_members(all_users_group_id, dave['peer_id'], db)
        member_names = [m['name'] for m in members]

        assert 'Alice' in member_names, "Dave should see Alice"
        assert 'Dave' in member_names, "Dave should see himself"
        assert 'Bob' not in member_names, "Dave should NOT see banned Bob"
        assert 'Charlie' not in member_names, "Dave should NOT see banned Charlie"

    assert_eventually(dave_sees_correct_state, db=db, start_t_ms=t_ms + 1000)


def test_rapid_join_ban_join_cycle(fresh_db):
    """Test corner case: rapid cycle of join, ban, join."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    t_ms = 2000

    # Multiple cycles of invite->join->ban
    for i in range(3):
        # Create invite
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
        t_ms += 100

        # User joins
        user_peer_id = peer.create(t_ms=t_ms, db=db)
        joined_user = user.join(peer_id=user_peer_id, invite_link=invite_link, name=f'User{i}', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()

        # Wait for user to join
        def user_joined(u=joined_user):
            channels = db.query_all(
                "SELECT channel_id FROM channels WHERE recorded_by = ?",
                (u['peer_id'],)
            )
            assert len(channels) >= 1

        t_ms = assert_eventually(user_joined, db=db, start_t_ms=t_ms)

        # Ban the user
        user_removed.create(
            removed_user_id=joined_user['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )
        db.commit()

        # Wait for ban
        def user_banned(u=joined_user):
            all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
            members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
            assert u['user_id'] not in [m['user_id'] for m in members]

        t_ms = assert_eventually(user_banned, db=db, start_t_ms=t_ms)

    # Now final user joins
    _, final_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
    t_ms += 100

    final_peer_id = peer.create(t_ms=t_ms, db=db)
    final_user = user.join(peer_id=final_peer_id, invite_link=final_invite_link, name='FinalUser', t_ms=t_ms, db=db)
    db.commit()

    # Wait for final user to see correct state
    def final_user_sees_correct_state():
        all_users_group_id = network.get_all_users_group_id(final_user['network_id'], final_user['peer_id'], db)
        members = group_member.list_members(all_users_group_id, final_user['peer_id'], db)
        member_names = [m['name'] for m in members]

        assert 'Alice' in member_names, "FinalUser should see Alice"
        assert 'FinalUser' in member_names, "FinalUser should see himself"
        # All previously banned users should not appear
        for i in range(3):
            assert f'User{i}' not in member_names, f"FinalUser should NOT see banned User{i}"

    assert_eventually(final_user_sees_correct_state, db=db, start_t_ms=t_ms)
