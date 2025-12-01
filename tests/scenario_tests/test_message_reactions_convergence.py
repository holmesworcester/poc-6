"""
Convergence tests for message emoji reactions.

Tests that reactions converge to identical state regardless of:
- Event arrival order
- Simultaneous reactions from multiple devices
- Toggle on/off/on sequences
- Out-of-order deletions

Uses convergence testing utilities to verify deterministic projection.
"""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite, peer as peer_module
from events.content import message, message_deletion, message_reaction
from events.group import group_member
import tick
from tests.utils import tick_helper, assert_reprojection, assert_idempotency, assert_convergence


def test_message_reactions_convergence_basic(fresh_db):
    """Test basic convergence: Alice reacts, Bob reacts, reactions converge regardless of order."""

    db = fresh_db
    tick.reset_state(db)

    print("\n=== Setup: Alice and Bob in network ===")

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db, network_name='test-network')
    alice_peer_id = alice['peer_id']

    # Create invite for Bob
    invite_id, invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=1500, db=db)

    # Bob joins
    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_id = bob['peer_id']

    db.commit()

    # Sync to convergence
    final_t_ms, rounds, converged, _ = tick_helper.sync_until_converged(
        db=db, start_t_ms=4000, max_rounds=50, check_interval=5, verbose=False
    )
    assert converged, "Peers should converge after initial sync"

    print(f"✓ Initial sync converged in {rounds} rounds")

    # Get channel
    from events.content import channel as channel_module
    alice_channels = channel_module.list_channels(recorded_by=alice_peer_id, db=db)
    channel_id = alice_channels[0]['channel_id']

    # Alice sends message
    print("\n=== Alice sends message ===")
    result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="Test message for reactions",
        t_ms=5000,
        db=db
    )
    message_id = result['id']
    db.commit()

    # Sync message
    final_t_ms, rounds, converged, _ = tick_helper.sync_until_converged(
        db=db, start_t_ms=6000, max_rounds=20, check_interval=5, verbose=False
    )
    assert converged, "Message should sync"

    # Both add reactions
    print("\n=== Alice and Bob add reactions ===")
    alice_reaction = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=7000,
        db=db
    )
    bob_reaction = message_reaction.create(
        peer_id=bob_peer_id,
        message_id=message_id,
        emoji='❤️',
        t_ms=7100,
        db=db
    )
    db.commit()

    print(f"✓ Reactions created: alice={alice_reaction[:20]}..., bob={bob_reaction[:20]}...")

    # Verify scenario
    print("\n=== Verify scenario ===")
    alice_msgs = message.list(channel_id, alice_peer_id, db)
    reactions = alice_msgs[0].get('reactions', [])
    assert len(reactions) == 2, f"Should have 2 reactions, got {len(reactions)}"
    print("✓ Scenario verified: Both reactions present")

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


def test_message_reactions_convergence_toggle(fresh_db):
    """Test convergence with toggle sequence: react → unreact → react"""

    db = fresh_db
    tick.reset_state(db)

    print("\n=== Setup: Two peers ===")

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db, network_name='test-network')
    alice_peer_id = alice['peer_id']

    # Bob joins
    invite_id, invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=1500, db=db)
    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_id = bob['peer_id']

    db.commit()

    # Sync
    final_t_ms, rounds, converged, _ = tick_helper.sync_until_converged(
        db=db, start_t_ms=4000, max_rounds=50, check_interval=5, verbose=False
    )
    assert converged

    # Create message
    from events.content import channel as channel_module
    channels = channel_module.list_channels(recorded_by=alice_peer_id, db=db)
    channel_id = channels[0]['channel_id']

    result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="Toggle test",
        t_ms=5000,
        db=db
    )
    message_id = result['id']
    db.commit()

    # Sync message
    final_t_ms, rounds, converged, _ = tick_helper.sync_until_converged(
        db=db, start_t_ms=6000, max_rounds=20, check_interval=5, verbose=False
    )

    # Toggle sequence: react → unreact → react
    print("\n=== Alice reacts (gc=1) ===")
    react_1 = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=7000,
        db=db
    )

    print("=== Alice unreacts ===")
    del_1 = message_reaction.remove(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=7100,
        db=db
    )

    print("=== Alice reacts again (gc=2) ===")
    react_2 = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=7200,
        db=db
    )

    db.commit()

    print(f"✓ Toggle sequence: react(gc=1) → unreact → react(gc=2)")

    # Verify scenario
    print("\n=== Verify scenario ===")
    alice_msgs = message.list(channel_id, alice_peer_id, db)
    msg = alice_msgs[0]
    reactions = msg.get('reactions', [])
    assert len(reactions) == 1, "Should have 1 reaction"
    assert reactions[0]['emoji'] == '👍', "Should be thumbs up"
    print("✓ Scenario verified: reaction present (gc=2)")

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


def test_message_reactions_convergence_cascade_deletion(fresh_db):
    """Test convergence with cascade deletion: message deleted removes all reactions"""

    db = fresh_db
    tick.reset_state(db)

    print("\n=== Setup: Two peers ===")

    alice = user.new_network(name='Alice', t_ms=1000, db=db, network_name='test-network')
    alice_peer_id = alice['peer_id']

    invite_id, invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=1500, db=db)
    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_id = bob['peer_id']

    db.commit()

    # Sync
    final_t_ms, rounds, converged, _ = tick_helper.sync_until_converged(
        db=db, start_t_ms=4000, max_rounds=50, check_interval=5, verbose=False
    )

    # Create message
    from events.content import channel as channel_module
    channels = channel_module.list_channels(recorded_by=alice_peer_id, db=db)
    channel_id = channels[0]['channel_id']

    result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="To be deleted",
        t_ms=5000,
        db=db
    )
    message_id = result['id']
    db.commit()

    # Sync message
    final_t_ms, rounds, converged, _ = tick_helper.sync_until_converged(
        db=db, start_t_ms=6000, max_rounds=20, check_interval=5, verbose=False
    )

    # Both add reactions
    print("\n=== Both add reactions ===")
    alice_react = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=7000,
        db=db
    )
    bob_react = message_reaction.create(
        peer_id=bob_peer_id,
        message_id=message_id,
        emoji='❤️',
        t_ms=7100,
        db=db
    )
    db.commit()

    # Sync reactions
    final_t_ms, rounds, converged, _ = tick_helper.sync_until_converged(
        db=db, start_t_ms=8000, max_rounds=20, check_interval=5, verbose=False
    )

    # Delete message (cascade deletes reactions)
    print("\n=== Alice deletes message (cascade) ===")
    msg_del = message_deletion.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        t_ms=9000,
        db=db
    )
    db.commit()

    print(f"✓ Message deleted: {msg_del[:20]}...")

    # Verify scenario
    print("\n=== Verify scenario ===")
    alice_msgs = message.list(channel_id, alice_peer_id, db)
    assert len(alice_msgs) == 0, "No messages should exist after cascade delete"
    print("✓ Scenario verified: no messages, no reactions")

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
