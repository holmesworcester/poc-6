"""
Convergence tests for message emoji reactions - SINGLE PEER focus.

Tests the reactions projection logic (global_count + deletion blocking)
without requiring multi-peer sync complexity.

Focuses on:
- Event reordering convergence
- Idempotency (multiple projections)
- Reprojection (rebuild from event store)
- Toggle sequences with out-of-order events
"""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user
from events.content import channel, message, message_deletion, message_reaction
from tests.utils import assert_reprojection, assert_idempotency, assert_convergence


def test_reactions_convergence_toggle_sequence(fresh_db):
    """Test convergence of toggle sequence: react(gc=1) → delete → react(gc=2)

    This is the CRITICAL test for global_count + deletion blocking:
    - Events may arrive out of order
    - Deletion blocking prevents gc=1 from being revived
    - Highest gc always wins
    - event_id (lexicographic order) breaks ties deterministically
    """

    db = fresh_db

    print("\n=== Setup: Single peer Alice ===")

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db, network_name='test-network')
    alice_peer_id = alice['peer_id']
    channel_id = alice['channel_id']

    db.commit()

    print("✓ Alice's network created")

    # Alice sends a message
    print("\n=== Alice sends message ===")
    result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="Test message for toggle convergence",
        t_ms=2000,
        db=db
    )
    message_id = result['id']
    print(f"✓ Message created: {message_id[:20]}...")

    db.commit()

    # Create toggle sequence: react → unreact → react
    print("\n=== Create toggle sequence ===")

    react_1 = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=3000,
        db=db
    )
    print(f"1. React (gc=1): {react_1[:20]}...")
    db.commit()

    # Project reaction so remove() can find it
    message_reaction.project(react_1, alice_peer_id, 3000, db)

    unreact = message_reaction.remove(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=4000,
        db=db
    )
    print(f"2. Unreact: {unreact[:20]}...")
    db.commit()

    # Project deletion event
    message_reaction.project_deletion(unreact, alice_peer_id, 4000, db)

    react_2 = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=5000,
        db=db
    )
    print(f"3. React again (gc=2): {react_2[:20]}...")
    db.commit()

    # Project reaction event
    message_reaction.project(react_2, alice_peer_id, 5000, db)

    # Verify final state is gc=2 (ON)
    print("\n=== Verify scenario ===")
    msgs = message.list(channel_id, alice_peer_id, db)
    assert len(msgs) > 0, "Message should exist"
    msg = msgs[0]
    reactions = msg.get('reactions', [])

    print(f"Message has {len(reactions)} reaction(s)")
    assert len(reactions) == 1, f"Should have 1 reaction, got {len(reactions)}"
    assert reactions[0]['emoji'] == '👍', "Should be thumbs up"
    print(f"✓ Scenario verified: Reaction present (gc=2, ON)")

    # Reprojection test: verify we can restore from event store
    print("\n=== REPROJECTION TEST ===")
    assert_reprojection(db)
    print("✓ Reprojection passed: State rebuilt from event store matches original")

    # Idempotency test: verify projection can be repeated without changing state
    print("\n=== IDEMPOTENCY TEST ===")
    assert_idempotency(db, num_trials=10, max_repetitions=5)
    print("✓ Idempotency passed: Multiple projections produce same state")

    # Convergence test: verify projection is order-independent
    print("\n=== CONVERGENCE TEST ===")
    assert_convergence(db)
    print("✓ Convergence passed: All event orderings converge to same state")


def test_reactions_convergence_multiple_emojis(fresh_db):
    """Test convergence with multiple different emoji reactions.

    Tests that multiple reactions converge correctly regardless of event order.
    """

    db = fresh_db

    print("\n=== Setup: Single peer ===")

    alice = user.new_network(name='Alice', t_ms=1000, db=db, network_name='test-network')
    alice_peer_id = alice['peer_id']
    channel_id = alice['channel_id']

    db.commit()

    # Create message
    result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="Multi-reaction test",
        t_ms=2000,
        db=db
    )
    message_id = result['id']

    db.commit()

    # Create multiple different emoji reactions
    print("\n=== Create multiple reactions ===")

    react_thumbs = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=3000,
        db=db
    )
    print(f"1. Thumbs up: {react_thumbs[:20]}...")

    react_heart = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='❤️',
        t_ms=3100,
        db=db
    )
    print(f"2. Heart: {react_heart[:20]}...")

    react_laugh = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='😂',
        t_ms=3200,
        db=db
    )
    print(f"3. Laugh: {react_laugh[:20]}...")

    db.commit()

    # Verify scenario
    print("\n=== Verify scenario ===")
    msgs = message.list(channel_id, alice_peer_id, db)
    reactions = msgs[0].get('reactions', [])

    print(f"Message has {len(reactions)} reactions")
    assert len(reactions) == 3, f"Should have 3 reactions, got {len(reactions)}"
    emojis = {r['emoji'] for r in reactions}
    assert emojis == {'👍', '❤️', '😂'}, f"Wrong emojis: {emojis}"
    print(f"✓ Scenario verified: All expected emojis present")

    # Reprojection test
    print("\n=== REPROJECTION TEST ===")
    assert_reprojection(db)
    print("✓ Reprojection passed")

    # Idempotency test
    print("\n=== IDEMPOTENCY TEST ===")
    assert_idempotency(db, num_trials=10, max_repetitions=5)
    print("✓ Idempotency passed")

    # Convergence test
    print("\n=== CONVERGENCE TEST ===")
    assert_convergence(db)
    print("✓ Convergence passed")


def test_reactions_convergence_cascade_deletion(fresh_db):
    """Test convergence of cascade deletion: message deleted → reactions deleted"""

    db = fresh_db

    print("\n=== Setup: Single peer ===")

    alice = user.new_network(name='Alice', t_ms=1000, db=db, network_name='test-network')
    alice_peer_id = alice['peer_id']
    channel_id = alice['channel_id']

    db.commit()

    # Create message and add reactions
    result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="To be deleted",
        t_ms=2000,
        db=db
    )
    message_id = result['id']

    db.commit()

    # Add reactions
    print("\n=== Add reactions ===")

    react_1 = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=3000,
        db=db
    )
    print(f"1. Thumbs up: {react_1[:20]}...")

    react_2 = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='❤️',
        t_ms=3100,
        db=db
    )
    print(f"2. Heart: {react_2[:20]}...")

    db.commit()

    # Delete message (cascade deletes reactions)
    print("\n=== Delete message (cascade) ===")
    msg_del = message_deletion.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        t_ms=4000,
        db=db
    )
    print(f"Message deleted: {msg_del[:20]}...")

    db.commit()

    # Verify scenario - message and reactions should be gone
    print("\n=== Verify scenario ===")
    msgs = message.list(channel_id, alice_peer_id, db)
    print(f"Message list has {len(msgs)} messages")
    assert len(msgs) == 0, f"Should have no messages after cascade delete, got {len(msgs)}"
    print("✓ Scenario verified: Message and reactions deleted")

    # Reprojection test
    print("\n=== REPROJECTION TEST ===")
    assert_reprojection(db)
    print("✓ Reprojection passed")

    # Idempotency test
    print("\n=== IDEMPOTENCY TEST ===")
    assert_idempotency(db, num_trials=10, max_repetitions=5)
    print("✓ Idempotency passed")

    # Convergence test
    print("\n=== CONVERGENCE TEST ===")
    assert_convergence(db)
    print("✓ Convergence passed")


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
