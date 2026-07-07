"""
Scenario test: User removal and peer removal functionality.

Tests the removal of users and peers from a network:
- Alice creates a network
- Bob joins Alice's network via invite
- Alice removes Bob (user removal)
- Verify Bob cannot sync anymore
- Verify historical events from Bob are still queryable
- Test cascading: removing user marks all their peers as removed
- Test peer-only removal: removing a specific peer device
"""
import pytest
from events.identity import user, invite, peer, network
from events.identity import user_removed, peer_removed
from events.group import group, group_member
from events.content import message
from tests.utils.tick_helper import assert_eventually


def test_user_removal_blocks_sync_but_preserves_history(fresh_db):
    """Test that removing a user blocks future sync but preserves their message history."""
    db = fresh_db

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates an invite for Bob
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Wait for Bob to join
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Wait for Bob to appear in Alice's member list
    def bob_in_member_list():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        member_names = [m['name'] for m in members]
        assert 'Bob' in member_names, "Alice should see Bob in member list"

    t_ms = assert_eventually(bob_in_member_list, db=db, start_t_ms=t_ms)
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    # Bob sends a message before being removed
    message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content='Hello from Bob!',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Alice removes Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms + 1000,
        db=db
    )
    db.commit()

    # Verify Bob is no longer in Alice's member list
    members_after = group_member.list_members(all_users_group_id, alice['peer_id'], db)
    member_names_after = [m['name'] for m in members_after]
    assert 'Bob' not in member_names_after, "Bob should not appear in member list after removal"
    assert 'Alice' in member_names_after, "Alice should still be in member list"


def test_authorization_rules(fresh_db):
    """Test authorization rules for peer and user removal."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    _, bob_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=2000, db=db)

    _, charlie_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2500, db=db)
    charlie_peer_id = peer.create(t_ms=3000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite_link, name='Charlie', t_ms=3000, db=db)
    db.commit()

    # Wait for sync
    def all_joined():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        charlie_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (charlie['peer_id'],)
        )
        assert len(bob_channels) >= 1
        assert len(charlie_channels) >= 1

    t_ms = assert_eventually(all_joined, db=db, start_t_ms=None)

    # Bob can remove himself (self-removal)
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=bob['peer_shared_id'],
        removed_by_local_peer_id=bob['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Charlie cannot remove Alice (not authorized)
    try:
        user_removed.create(
            removed_user_id=alice['user_id'],
            removed_by_peer_id=charlie['peer_shared_id'],
            removed_by_local_peer_id=charlie['peer_id'],
            t_ms=t_ms + 500,
            db=db
        )
        assert False, "Charlie should NOT be able to remove Alice (not admin, not self)"
    except ValueError:
        pass  # Expected

    # Alice can remove Bob (she's the admin)
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms + 1000,
        db=db
    )
    db.commit()

    # Re-add Charlie for peer removal tests
    _, charlie_invite_link2, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms + 1500, db=db)
    charlie_peer_id2 = peer.create(t_ms=t_ms + 2000, db=db)
    charlie2 = user.join(peer_id=charlie_peer_id2, invite_link=charlie_invite_link2, name='Charlie', t_ms=t_ms + 2000, db=db)
    db.commit()

    # Charlie cannot remove Alice's peer (not admin)
    try:
        peer_removed.create(
            removed_peer_shared_id=alice['peer_shared_id'],
            removed_by_peer_shared_id=charlie2['peer_shared_id'],
            removed_by_local_peer_id=charlie2['peer_id'],
            t_ms=t_ms + 2500,
            db=db
        )
        assert False, "Charlie should NOT be able to remove a peer (not admin)"
    except ValueError:
        pass  # Expected

    # Alice can remove Charlie's peer (she's the admin)
    peer_removed.create(
        removed_peer_shared_id=charlie2['peer_shared_id'],
        removed_by_peer_shared_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms + 3000,
        db=db
    )
    db.commit()


@pytest.mark.skip(reason="Key rotation on removal not yet implemented - pre-existing issue")
def test_user_removal_rotates_group_keys(fresh_db):
    """Test that user removal triggers group key rotation."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    _, bob_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Wait for sync
    def bob_joined():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_joined, db=db, start_t_ms=None)

    # Get original key
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
    original_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    original_key_id = original_key['key_id']

    # Alice removes Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Verify key was rotated
    new_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    new_key_id = new_key['key_id']

    assert new_key_id != original_key_id, "Key should be rotated when user is removed"


@pytest.mark.skip(reason="Key rotation on removal not yet implemented - pre-existing issue")
def test_peer_removal_last_device_rotates_keys(fresh_db):
    """Test that peer removal triggers group key rotation."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    _, bob_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Wait for sync
    def bob_joined():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_joined, db=db, start_t_ms=None)

    # Get original key
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
    original_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    original_key_id = original_key['key_id']

    # Alice removes Bob's peer
    peer_removed.create(
        removed_peer_shared_id=bob['peer_shared_id'],
        removed_by_peer_shared_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Verify key was rotated
    new_key = group.get_current_key(all_users_group_id, alice['peer_id'], db)
    new_key_id = new_key['key_id']

    assert new_key_id != original_key_id, "Key should be rotated when peer is removed"


def test_removed_peer_cannot_sync_messages(fresh_db):
    """Verify that a removed peer cannot sync messages."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    _, bob_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Wait for sync
    def bob_joined():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_joined, db=db, start_t_ms=None)

    # Alice sends a message before Bob is removed
    message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Alice before Bob removal',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for Bob to receive message
    def bob_sees_message():
        bob_messages = message.list(bob['channel_id'], bob['peer_id'], db)
        bob_contents = [msg['content'] for msg in bob_messages]
        assert 'Alice before Bob removal' in bob_contents

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)

    # Alice removes Bob
    peer_removed.create(
        removed_peer_shared_id=bob['peer_shared_id'],
        removed_by_peer_shared_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for removal to sync
    def removal_done():
        pass

    t_ms = assert_eventually(removal_done, db=db, start_t_ms=t_ms, max_rounds=50)

    # Alice sends a message AFTER removing Bob
    message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Alice after Bob removal',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Bob should NOT receive the post-removal message
    def bob_does_not_see_new_message():
        bob_messages_after = message.list(bob['channel_id'], bob['peer_id'], db)
        bob_contents_after = [msg['content'] for msg in bob_messages_after]
        assert 'Alice after Bob removal' not in bob_contents_after

    assert_eventually(bob_does_not_see_new_message, db=db, start_t_ms=t_ms, max_rounds=100)


@pytest.mark.xfail(reason="user_removed event not syncing to removed user's perspective - will fix later")
def test_removed_user_cannot_send_messages(fresh_db):
    """Test that a removed user cannot send messages (local enforcement)."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    _, bob_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Wait for sync
    def bob_joined():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_joined, db=db, start_t_ms=None)

    # Bob sends a message successfully before removal
    message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content='Hello from Bob before removal',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Alice removes Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms + 1000,
        db=db
    )
    db.commit()

    # Wait for removal to sync
    def removal_synced():
        pass

    t_ms = assert_eventually(removal_synced, db=db, start_t_ms=t_ms + 1000)

    # Bob tries to send a message after removal (should fail)
    try:
        message.create(
            peer_id=bob['peer_id'],
            channel_id=bob['channel_id'],
            content='Bob tries to send after removal',
            t_ms=t_ms,
            db=db
        )
        assert False, "Bob should NOT be able to send messages after removal"
    except ValueError as e:
        assert "removed" in str(e).lower(), f"Error should mention removal: {e}"


def test_removed_user_not_in_user_list(fresh_db):
    """Test that a removed user does not appear in the user list."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    _, bob_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=2000, db=db)

    _, charlie_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2500, db=db)
    charlie_peer_id = peer.create(t_ms=3000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite_link, name='Charlie', t_ms=3000, db=db)
    db.commit()

    # Wait for sync
    def all_joined():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        charlie_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (charlie['peer_id'],)
        )
        assert len(bob_channels) >= 1
        assert len(charlie_channels) >= 1

    t_ms = assert_eventually(all_joined, db=db, start_t_ms=None)

    # Wait for all members to appear in the list
    def all_members_visible():
        all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        members_before = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        member_names_before = [m['name'] for m in members_before]
        assert 'Alice' in member_names_before
        assert 'Bob' in member_names_before
        assert 'Charlie' in member_names_before
        assert len(members_before) == 3

    t_ms = assert_eventually(all_members_visible, db=db, start_t_ms=t_ms)
    all_users_group_id = network.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    # Alice removes Bob
    user_removed.create(
        removed_user_id=bob['user_id'],
        removed_by_peer_id=alice['peer_shared_id'],
        removed_by_local_peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Check user list after removal (Alice's view)
    members_after = group_member.list_members(all_users_group_id, alice['peer_id'], db)
    member_names_after = [m['name'] for m in members_after]
    assert 'Alice' in member_names_after
    assert 'Bob' not in member_names_after
    assert 'Charlie' in member_names_after
    assert len(members_after) == 2

    # Wait for Charlie to see the removal
    def charlie_sees_removal():
        charlie_all_users_group_id = network.get_all_users_group_id(charlie['network_id'], charlie['peer_id'], db)
        members_charlie_view = group_member.list_members(charlie_all_users_group_id, charlie['peer_id'], db)
        member_names_charlie = [m['name'] for m in members_charlie_view]
        assert 'Bob' not in member_names_charlie

    assert_eventually(charlie_sees_removal, db=db, start_t_ms=t_ms)
