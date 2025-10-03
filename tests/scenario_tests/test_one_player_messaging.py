"""
Scenario test: Alice sends messages to herself and can see them.

Tests the full event flow using API-style commands and queries only.
No direct database inspection - all verification via returned data and query functions.
"""
import sqlite3
import pytest
from db import Database
import schema
from events import user, channel, message


def test_alice_sends_to_herself():
    """Alice creates her identity, a group, a channel, and sends messages to herself."""

    # Setup: Initialize in-memory database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)

    # Create all tables using centralized schema loader
    schema.create_all(db)

    # Test flow: Alice creates network (peer, prekey, key, group, channel, user, invite)
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Verify all components were created
    assert len(alice['peer_id']) == 24
    assert len(alice['peer_shared_id']) == 24
    assert len(alice['prekey_id']) == 24
    assert len(alice['prekey_shared_id']) == 24
    assert len(alice['key_id']) == 24
    assert len(alice['group_id']) == 24
    assert len(alice['channel_id']) == 24
    assert len(alice['user_id']) == 24
    assert alice['invite_link'].startswith('quiet://')

    # Send first message in default channel
    result1 = message.create_message(
        params={
            'content': 'Hello',
            'channel_id': alice['channel_id'],
            'group_id': alice['group_id'],
            'peer_id': alice['peer_id'],
            'peer_shared_id': alice['peer_shared_id'],
            'key_id': alice['key_id']
        },
        t_ms=2000,
        db=db
    )

    # Verify message was created and appears in latest
    assert 'id' in result1
    assert len(result1['id']) == 24
    assert 'latest' in result1
    assert len(result1['latest']) == 1
    assert result1['latest'][0]['content'] == 'Hello'
    assert result1['latest'][0]['author_id'] == alice['peer_shared_id']

    # Send second message
    result2 = message.create_message(
        params={
            'content': 'World',
            'channel_id': alice['channel_id'],
            'group_id': alice['group_id'],
            'peer_id': alice['peer_id'],
            'peer_shared_id': alice['peer_shared_id'],
            'key_id': alice['key_id']
        },
        t_ms=3000,
        db=db
    )

    # Verify both messages appear in latest
    assert len(result2['latest']) == 2
    assert result2['latest'][0]['content'] == 'World'  # Most recent first
    assert result2['latest'][1]['content'] == 'Hello'

    # Create a second channel for realism
    random_channel_id = channel.create(
        name='random',
        group_id=alice['group_id'],
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        key_id=alice['key_id'],
        t_ms=4000,
        db=db
    )

    # Send message to second channel
    result3 = message.create_message(
        params={
            'content': 'Random thoughts',
            'channel_id': random_channel_id,
            'group_id': alice['group_id'],
            'peer_id': alice['peer_id'],
            'peer_shared_id': alice['peer_shared_id'],
            'key_id': alice['key_id']
        },
        t_ms=5000,
        db=db
    )

    # Verify message only appears in random channel
    assert len(result3['latest']) == 1
    assert result3['latest'][0]['content'] == 'Random thoughts'

    # Query messages from general channel
    general_messages = message.list_messages(alice['channel_id'], alice['peer_id'], db)
    assert len(general_messages) == 2
    assert general_messages[0]['content'] == 'World'
    assert general_messages[1]['content'] == 'Hello'
    assert all(m['channel_id'] == alice['channel_id'] for m in general_messages)

    # Query messages from random channel
    random_messages = message.list_messages(random_channel_id, alice['peer_id'], db)
    assert len(random_messages) == 1
    assert random_messages[0]['content'] == 'Random thoughts'
    assert random_messages[0]['channel_id'] == random_channel_id

    # Verify channels are queryable
    channels_list = channel.list_channels(alice['peer_id'], db)
    assert len(channels_list) == 2
    channel_names = {c['name'] for c in channels_list}
    assert 'general' in channel_names
    assert 'random' in channel_names

    # Verify groups are queryable
    from events import group
    groups_list = group.list_all_groups(alice['peer_id'], db)
    assert len(groups_list) == 1
    assert groups_list[0]['name'] == "Alice's Network"
    assert groups_list[0]['group_id'] == alice['group_id']

    print("✓ All tests passed! Alice can send messages to herself and see them.")

    # Re-projection test: verify we can restore from event store
    from tests.utils import assert_reprojection
    assert_reprojection(db)

    # Idempotency test: verify projection can be repeated without changing state
    from tests.utils import assert_idempotency
    assert_idempotency(db, num_trials=10, max_repetitions=5)

    # Convergence test: verify projection is order-independent
    from tests.utils import assert_convergence
    assert_convergence(db)


if __name__ == '__main__':
    test_alice_sends_to_herself()
