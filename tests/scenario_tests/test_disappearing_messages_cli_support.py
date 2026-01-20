"""
Scenario tests for disappearing messages CLI support.

Tests that the API returns the fields needed by the CLI:
- channel.list() returns disappearing_time_ms
- message.list() returns ttl_ms
- channel_update works for turning off disappearing messages
- Non-admin cannot update channel disappearing time
"""
import pytest
from events.identity import user, invite, peer as peer_module
from events.content import channel, message, channel_update
from core import tick


def test_channel_list_returns_disappearing_time_ms(fresh_db):
    """channel.list() includes disappearing_time_ms field."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create channel with disappearing time
    channel_id = channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db,
        disappearing_time_ms=7 * 24 * 60 * 60 * 1000  # 7 days
    )
    db.commit()

    # List channels and verify field is present
    channels = channel.list(alice['peer_id'], db)

    ephemeral_ch = next((c for c in channels if c['channel_id'] == channel_id), None)
    assert ephemeral_ch is not None
    assert 'disappearing_time_ms' in ephemeral_ch
    assert ephemeral_ch['disappearing_time_ms'] == 7 * 24 * 60 * 60 * 1000

    # Default channel (#general) should have 0
    general_ch = next((c for c in channels if c['name'] == 'general'), None)
    assert general_ch is not None
    assert general_ch['disappearing_time_ms'] == 0


def test_message_list_returns_ttl_ms(fresh_db):
    """message.list() includes ttl_ms field."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create channel with disappearing time
    channel_id = channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db,
        disappearing_time_ms=5000  # 5 seconds
    )
    db.commit()

    # Send message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Test message",
        t_ms=3000,
        db=db
    )
    db.commit()

    # List messages and verify ttl_ms is present
    messages = message.list(channel_id, alice['peer_id'], db)

    assert len(messages) == 1
    assert 'ttl_ms' in messages[0]
    # TTL should be created_at + disappearing_time_ms = 3000 + 5000 = 8000
    assert messages[0]['ttl_ms'] == 8000


def test_message_in_permanent_channel_has_zero_ttl(fresh_db):
    """Messages in channels without disappearing have ttl_ms = 0."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Get #general channel (created by new_network)
    channels = channel.list(alice['peer_id'], db)
    general_ch = next((c for c in channels if c['name'] == 'general'), None)

    # Send message to permanent channel
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=general_ch['channel_id'],
        content="Permanent message",
        t_ms=2000,
        db=db
    )
    db.commit()

    # List messages and verify ttl_ms is 0
    messages = message.list(general_ch['channel_id'], alice['peer_id'], db)

    assert len(messages) == 1
    assert messages[0]['ttl_ms'] == 0


def test_channel_update_turn_off_disappearing(fresh_db):
    """Setting disappearing_time_ms to 0 turns it off."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create channel with disappearing time
    channel_id = channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db,
        disappearing_time_ms=5000
    )
    db.commit()

    # Verify it's set
    channels = channel.list(alice['peer_id'], db)
    ch = next(c for c in channels if c['channel_id'] == channel_id)
    assert ch['disappearing_time_ms'] == 5000

    # Turn it off
    update_id = channel_update.create(
        channel_id=channel_id,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3000,
        db=db,
        new_disappearing_time_ms=0
    )
    # Projection happens automatically via store.event() in create()
    db.commit()

    # Verify it's off
    channels = channel.list(alice['peer_id'], db)
    ch = next(c for c in channels if c['channel_id'] == channel_id)
    assert ch['disappearing_time_ms'] == 0


def test_non_admin_cannot_update_channel_disappearing(fresh_db):
    """Non-admin user cannot call channel_update.create()."""
    db = fresh_db

    # Alice creates network (is admin)
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite for Bob
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins (not admin)
    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Sync so Bob has the channel
    for i in range(10):
        tick.tick(t_ms=2100 + i * 100, db=db)
    db.commit()

    # Get general channel
    channels = channel.list(alice['peer_id'], db)
    general_ch = next(c for c in channels if c['name'] == 'general')

    # Bob tries to update channel - should fail
    with pytest.raises(ValueError) as exc_info:
        channel_update.create(
            channel_id=general_ch['channel_id'],
            peer_id=bob['peer_id'],
            peer_shared_id=bob['peer_shared_id'],
            t_ms=3000,
            db=db,
            new_disappearing_time_ms=5000
        )

    assert "not authorized" in str(exc_info.value).lower() or "admin" in str(exc_info.value).lower()
