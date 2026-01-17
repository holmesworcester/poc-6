"""
Scenario tests for channel API functions.

Tests backend functions used by CLI:
- channel.list_with_keys() returns channels with key_id
- channel.get() returns single channel by ID
"""
import pytest
from events.identity import user, invite, peer as peer_module
from events.content import channel
from events.group import group_key
from core import tick


def test_list_channels_with_keys_returns_key_id(fresh_db):
    """channel.list_with_keys() includes key_id from group."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create additional channel
    channel_id = channel.create(
        name='testing',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db
    )
    db.commit()

    # List channels with keys
    channels = channel.list_with_keys(alice['peer_id'], db)

    # Should have at least 2 channels (general + testing)
    assert len(channels) >= 2

    # All channels should have key_id
    for ch in channels:
        assert 'key_id' in ch, f"Channel {ch['name']} missing key_id"
        assert ch['key_id'] is not None, f"Channel {ch['name']} has None key_id"
        assert 'name' in ch
        assert 'channel_id' in ch
        assert 'group_id' in ch

    # Verify testing channel specifically
    testing_ch = next((c for c in channels if c['name'] == 'testing'), None)
    assert testing_ch is not None
    assert testing_ch['channel_id'] == channel_id


def test_list_channels_with_keys_key_is_valid(fresh_db):
    """key_id from list_channels_with_keys() is a valid group key."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    channels = channel.list_with_keys(alice['peer_id'], db)
    general_ch = next((c for c in channels if c['name'] == 'general'), None)
    assert general_ch is not None

    # key_id should be retrievable via group_key.get_key()
    key_data = group_key.get_key(general_ch['key_id'], alice['peer_id'], db)
    assert key_data is not None
    assert 'key' in key_data


def test_list_channels_with_keys_includes_all_fields(fresh_db):
    """list_channels_with_keys() includes all standard channel fields plus key_id."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create channel with disappearing time
    channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db,
        disappearing_time_ms=86400000  # 1 day
    )
    db.commit()

    channels = channel.list_with_keys(alice['peer_id'], db)
    ephemeral_ch = next((c for c in channels if c['name'] == 'ephemeral'), None)
    assert ephemeral_ch is not None

    # Should have all standard fields
    assert 'channel_id' in ephemeral_ch
    assert 'name' in ephemeral_ch
    assert 'group_id' in ephemeral_ch
    assert 'signed_by' in ephemeral_ch
    assert 'created_at' in ephemeral_ch
    assert 'disappearing_time_ms' in ephemeral_ch
    # Plus the key_id from join
    assert 'key_id' in ephemeral_ch

    assert ephemeral_ch['disappearing_time_ms'] == 86400000


def test_get_by_id_returns_channel(fresh_db):
    """channel.get() returns channel dict for valid ID."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create a channel
    channel_id = channel.create(
        name='lookup-test',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db
    )
    db.commit()

    # Get by ID
    ch = channel.get(channel_id, alice['peer_id'], db)

    assert ch is not None
    assert ch['channel_id'] == channel_id
    assert ch['name'] == 'lookup-test'
    assert 'group_id' in ch
    assert 'signed_by' in ch
    assert 'created_at' in ch
    assert 'disappearing_time_ms' in ch


def test_get_by_id_returns_none_for_invalid_id(fresh_db):
    """channel.get() returns None for non-existent channel."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Try to get non-existent channel
    ch = channel.get('nonexistent-channel-id', alice['peer_id'], db)

    assert ch is None


def test_get_by_id_respects_recorded_by(fresh_db):
    """channel.get() only returns channels visible to the peer."""
    db = fresh_db

    # Alice creates network and channel
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    channel_id = channel.create(
        name='alice-channel',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db
    )
    db.commit()

    # Bob creates separate network (no sync between networks)
    bob = user.new_network(name='Bob', t_ms=3000, db=db)
    db.commit()

    # Alice can see her channel
    alice_view = channel.get(channel_id, alice['peer_id'], db)
    assert alice_view is not None
    assert alice_view['name'] == 'alice-channel'

    # Bob cannot see Alice's channel (different network, no sync)
    bob_view = channel.get(channel_id, bob['peer_id'], db)
    assert bob_view is None


def test_get_by_id_after_sync(fresh_db):
    """channel.get() works after channel syncs to new user."""
    db = fresh_db

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite for Bob
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Sync - use more ticks to ensure convergence
    for i in range(50):
        tick.tick(t_ms=2100 + i * 100, db=db)
    db.commit()

    # Verify Bob can list channels (prerequisite for get_by_id to work)
    bob_channels = channel.list(bob['peer_id'], db)
    assert len(bob_channels) > 0, "Bob should have synced channels"
    bob_general = next((c for c in bob_channels if c['name'] == 'general'), None)
    assert bob_general is not None, "Bob should see general channel via list_channels"

    # Now test get_by_id with the channel_id from Bob's perspective
    bob_view = channel.get(bob_general['channel_id'], bob['peer_id'], db)
    assert bob_view is not None
    assert bob_view['name'] == 'general'
