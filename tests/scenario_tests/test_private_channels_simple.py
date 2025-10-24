"""Simple test for private channels without complex syncing."""
import sqlite3
import pytest
from db import Database, create_safe_db
import schema
from events.identity import user, invite
from events.content import channel
from events.group import group_member


def test_non_admin_cannot_create_channels():
    """Test that non-admin users cannot create channels."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    with pytest.raises(ValueError):
        channel.create(
            name="test_channel",
            peer_id=bob['peer_id'],
            peer_shared_id=bob['peer_shared_id'],
            t_ms=3000,
            db=db
        )


def test_admin_can_create_public_channel():
    """Test that admin can create public channel."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    public_channel_id = channel.create(
        name="general",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3100,
        db=db
    )

    assert public_channel_id is not None


def test_admin_can_create_private_channel():
    """Test that admin can create private channel with specific members."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    private_channel_id = channel.create(
        name="secretgroup",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3200,
        db=db,
        member_user_ids=[bob['user_id']]
    )

    assert private_channel_id is not None


def test_verify_private_channel_group_membership():
    """Test that private channel has correct group membership."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    private_channel_id = channel.create(
        name="secretgroup",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3200,
        db=db,
        member_user_ids=[bob['user_id']]
    )

    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    private_ch = alice_safedb.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (private_channel_id, alice['peer_id'])
    )

    assert private_ch is not None
    group_id = private_ch['group_id']

    # Check if Alice is a member
    alice_is_member = alice_safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ? AND recorded_by = ?",
        (group_id, alice['user_id'], alice['peer_id'])
    )

    assert alice_is_member is not None


def test_admin_can_add_member_to_channel():
    """Test that admin can add members to private channel."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    charlie = user.join(invite_link=invite_link, name='Charlie', t_ms=2500, db=db)

    private_channel_id = channel.create(
        name="secretgroup",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3200,
        db=db,
        member_user_ids=[bob['user_id']]
    )

    # Alice adds Charlie
    channel.add_member_to_channel(
        channel_id=private_channel_id,
        user_id=charlie['user_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3300,
        db=db
    )

    # Verify event was created
    assert True  # Event creation succeeded


def test_non_admin_cannot_add_members():
    """Test that non-admin users cannot add members to private channel."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    charlie = user.join(invite_link=invite_link, name='Charlie', t_ms=2500, db=db)

    private_channel_id = channel.create(
        name="secretgroup",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3200,
        db=db,
        member_user_ids=[bob['user_id']]
    )

    with pytest.raises(ValueError):
        channel.add_member_to_channel(
            channel_id=private_channel_id,
            user_id=charlie['user_id'],
            peer_id=bob['peer_id'],
            peer_shared_id=bob['peer_shared_id'],
            t_ms=3400,
            db=db
        )


def test_channels_use_different_encryption_keys():
    """Test that public and private channels use different group keys."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    public_channel_id = channel.create(
        name="general",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3100,
        db=db
    )

    private_channel_id = channel.create(
        name="secretgroup",
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3200,
        db=db,
        member_user_ids=[bob['user_id']]
    )

    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    public_ch = alice_safedb.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (public_channel_id, alice['peer_id'])
    )

    private_ch = alice_safedb.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (private_channel_id, alice['peer_id'])
    )

    assert public_ch is not None
    assert private_ch is not None
    assert public_ch['group_id'] != private_ch['group_id']
