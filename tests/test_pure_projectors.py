"""Test pure functional projectors.

Compares pure projector output against existing projector behavior
to verify they produce the same results.
"""
import sqlite3
import pytest
from db import Database
import schema
import tick
from events.identity import user, invite, peer
from events.content import channel as channel_module, message as message_module
from tests.utils import tick_helper
import store
import crypto

from pure_projectors import resolver
from pure_projectors.message import project as pure_project_message
from pure_projectors.channel import project as pure_project_channel
from pure_projectors.group_member import project as pure_project_group_member


def setup_test_network(db):
    """Create a test network with Alice and Bob."""
    tick.reset_state(db)

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates invite for Bob
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    db.commit()

    # Sync to converge
    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=50)

    return alice, bob


def test_pure_message_projector():
    """Test that pure message projector produces same output as original."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice, bob = setup_test_network(db)

    # Alice sends a message
    result = message_module.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Hello from pure projector test!",
        t_ms=10000,
        db=db
    )
    message_id = result['id']
    db.commit()

    print(f"\n=== Testing pure message projector ===")
    print(f"Message ID: {message_id[:30]}...")

    # Get what the original projector produced
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    original_row = safedb.query_one(
        "SELECT * FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )

    assert original_row is not None, "Original projector should have created row"
    print(f"Original row: message_id={original_row['message_id'][:20]}..., content={original_row['content'][:30]}...")

    # Now test the pure projector
    input_dict = resolver.resolve_message(
        event_id=message_id,
        recorded_by=alice['peer_id'],
        recorded_at=10000,
        db=db
    )

    assert input_dict is not None, "Resolver should return input dict"
    print(f"Resolved input dict keys: {input_dict.keys()}")
    print(f"Dependencies: {list(input_dict['dependencies'].keys())}")

    # Run pure projector
    result = pure_project_message(input_dict)

    print(f"Pure projector result: valid={result.valid}, blocked={result.blocked}")
    print(f"Tables in output: {list(result.tables.keys())}")

    assert result.valid, f"Pure projector should be valid: {result.reason}"
    assert not result.blocked, f"Pure projector should not be blocked: {result.missing_deps}"
    assert "messages" in result.tables, "Should output to messages table"

    pure_row = result.tables["messages"][0]

    # Compare key fields
    assert pure_row["message_id"] == original_row["message_id"], "message_id should match"
    assert pure_row["channel_id"] == original_row["channel_id"], "channel_id should match"
    assert pure_row["content"] == original_row["content"], "content should match"
    assert pure_row["author_id"] == original_row["author_id"], "author_id should match"
    assert pure_row["group_id"] == original_row["group_id"], "group_id should match"

    print("SUCCESS: Pure message projector output matches original!")


def test_pure_channel_projector():
    """Test that pure channel projector produces same output as original."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice, bob = setup_test_network(db)

    print(f"\n=== Testing pure channel projector ===")

    # Use Alice's main channel
    channel_id = alice['channel_id']
    print(f"Channel ID: {channel_id[:30]}...")

    # Get what the original projector produced
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    original_row = safedb.query_one(
        "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (channel_id, alice['peer_id'])
    )

    assert original_row is not None, "Original projector should have created row"
    print(f"Original row: channel_id={original_row['channel_id'][:20]}..., name={original_row['name']}")

    # Test the pure projector
    input_dict = resolver.resolve_channel(
        event_id=channel_id,
        recorded_by=alice['peer_id'],
        recorded_at=1000,
        db=db
    )

    assert input_dict is not None, "Resolver should return input dict"
    print(f"Resolved input dict keys: {input_dict.keys()}")
    print(f"Dependencies: {list(input_dict['dependencies'].keys())}")

    # Run pure projector
    result = pure_project_channel(input_dict)

    print(f"Pure projector result: valid={result.valid}, blocked={result.blocked}")

    assert result.valid, f"Pure projector should be valid: {result.reason}"
    assert not result.blocked, f"Pure projector should not be blocked: {result.missing_deps}"
    assert "channels" in result.tables, "Should output to channels table"

    pure_row = result.tables["channels"][0]

    # Compare key fields
    assert pure_row["channel_id"] == original_row["channel_id"], "channel_id should match"
    assert pure_row["name"] == original_row["name"], "name should match"
    assert pure_row["group_id"] == original_row["group_id"], "group_id should match"

    print("SUCCESS: Pure channel projector output matches original!")


def test_pure_group_member_projector():
    """Test that pure group_member projector produces same output as original."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice, bob = setup_test_network(db)

    print(f"\n=== Testing pure group_member projector ===")

    # Find a REAL group_member event (not a user event used as member_id)
    from db import create_safe_db, create_unsafe_db
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    unsafedb = create_unsafe_db(db)

    # Get all group_member entries and find one that's actually a group_member event type
    members = safedb.query(
        "SELECT * FROM group_members WHERE recorded_by = ?",
        (alice['peer_id'],)
    )

    member_row = None
    for m in members:
        # Check if this member_id is actually a group_member event (not a user event)
        blob = store.get(m['member_id'], unsafedb)
        if blob:
            data, _ = crypto.unwrap(blob, alice['peer_id'], db)
            if data:
                event_data = crypto.parse_json(data)
                if event_data.get('type') == 'group_member':
                    member_row = m
                    break

    if not member_row:
        pytest.skip("No actual group_member event found - only bootstrap user events")

    member_id = member_row['member_id']
    print(f"Member ID: {member_id[:30]}...")
    print(f"Original row: group_id={member_row['group_id'][:20]}..., user_id={member_row['user_id'][:20]}...")

    # Test the pure projector
    input_dict = resolver.resolve_group_member(
        event_id=member_id,
        recorded_by=alice['peer_id'],
        recorded_at=member_row['recorded_at'],
        db=db
    )

    assert input_dict is not None, "Resolver should return input dict"
    print(f"Resolved input dict keys: {input_dict.keys()}")
    print(f"Event data: {input_dict['event_data']}")
    print(f"Dependencies: {input_dict['dependencies']}")

    # Run pure projector
    result = pure_project_group_member(input_dict)

    print(f"Pure projector result: valid={result.valid}, blocked={result.blocked}")

    assert result.valid, f"Pure projector should be valid: {result.reason}"
    assert not result.blocked, f"Pure projector should not be blocked: {result.missing_deps}"
    assert "group_members" in result.tables, "Should output to group_members table"

    pure_row = result.tables["group_members"][0]

    # Compare key fields
    assert pure_row["member_id"] == member_row["member_id"], "member_id should match"
    assert pure_row["group_id"] == member_row["group_id"], "group_id should match"
    assert pure_row["user_id"] == member_row["user_id"], "user_id should match"

    print("SUCCESS: Pure group_member projector output matches original!")


def test_message_projector_with_disappearing_messages():
    """Test that TTL is correctly calculated from channel's disappearing_time_ms."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice, bob = setup_test_network(db)

    print(f"\n=== Testing message TTL calculation ===")

    # Create a channel with disappearing messages enabled
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    # Get peer_shared_id for Alice
    peer_self = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (alice['peer_id'], alice['peer_id'])
    )
    alice_peer_shared_id = peer_self['peer_shared_id']

    # Create channel with 1 hour disappearing time
    disappearing_time_ms = 60 * 60 * 1000  # 1 hour

    channel_id = channel_module.create(
        name="Disappearing Channel",
        peer_id=alice['peer_id'],
        peer_shared_id=alice_peer_shared_id,
        t_ms=20000,
        db=db,
        disappearing_time_ms=disappearing_time_ms
    )
    db.commit()

    # Sync to project the channel
    tick_helper.sync_until_converged(db=db, start_t_ms=25000, max_rounds=20)

    # Send message
    message_created_at = 30000
    result = message_module.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="This message will disappear",
        t_ms=message_created_at,
        db=db
    )
    message_id = result['id']
    db.commit()

    # Test pure projector
    input_dict = resolver.resolve_message(
        event_id=message_id,
        recorded_by=alice['peer_id'],
        recorded_at=message_created_at,
        db=db
    )

    assert input_dict is not None
    assert input_dict['dependencies']['channel'] is not None

    channel_dep = input_dict['dependencies']['channel']
    print(f"Channel disappearing_time_ms: {channel_dep['event_data'].get('disappearing_time_ms')}")

    result = pure_project_message(input_dict)

    assert result.valid
    pure_row = result.tables["messages"][0]

    expected_ttl = message_created_at + disappearing_time_ms
    assert pure_row["ttl_ms"] == expected_ttl, f"TTL should be {expected_ttl}, got {pure_row['ttl_ms']}"

    print(f"SUCCESS: TTL correctly calculated as {pure_row['ttl_ms']}")


if __name__ == "__main__":
    test_pure_message_projector()
    test_pure_channel_projector()
    test_pure_group_member_projector()
    test_message_projector_with_disappearing_messages()
    print("\n=== All tests passed! ===")
