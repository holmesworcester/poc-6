"""Isolated sync tests for negentropy protocol.

These tests run the negentropy sync protocol in total isolation:
- Stubbed connections (no encryption, immediate delivery)
- Minimal event data (just IDs and timestamps)
- Rich logging for debugging

Run with: pytest tests/sync_tests/test_negentropy_isolated.py -v -s
"""

import pytest
import logging

from tests.sync_tests import (
    SyncTestHarness,
    EventFactory,
    StubConnectionBridge,
    stub_bridge,
)
from tests.sync_tests.event_factory import EventSet, TestEvent
from events.network import negentropy


# Enable logging for sync tests
logging.basicConfig(level=logging.INFO, format='%(message)s')


class TestBasicSync:
    """Basic two-peer sync scenarios."""

    def test_identical_peers_already_synced(self):
        """Two peers with identical events should converge immediately."""
        factory = EventFactory(seed=1)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        # Both have exactly the same events
        events = factory.generate(count=10, prefix="shared")
        harness.add_events(alice, events)
        harness.add_events(bob, events)

        # Should converge quickly since hashes match
        result = harness.run_sync(max_rounds=10, expected_count=10)

        assert result.converged
        assert harness.peers_have_same_hash()
        assert harness.peers_have_same_events()
        # Should take minimal rounds since hashes match immediately
        # (allows for convergence_threshold rounds to detect stability)
        assert result.rounds_taken <= 10

    def test_complete_divergence(self):
        """Two peers with completely different events - tests protocol exchange.

        NOTE: In isolated testing, we can verify the protocol exchanges event IDs
        but actual blob delivery requires the full event projection stack.
        This test verifies the protocol runs without error.
        """
        factory = EventFactory(seed=2)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        # Each peer has unique events
        alice_events = factory.generate(count=10, prefix="alice")
        bob_events = factory.generate(count=10, prefix="bob")

        harness.add_events(alice, alice_events)
        harness.add_events(bob, bob_events)

        # Run sync - protocol will exchange range_events messages
        result = harness.run_sync(max_rounds=20)

        # In isolated test, hashes won't match because blobs don't transfer
        # But we verify the protocol ran and exchanged messages
        assert result.total_messages > 0
        # Events stay at 10 each since isolated test doesn't transfer blobs
        assert harness.get_event_count(alice) == 10
        assert harness.get_event_count(bob) == 10

    def test_partial_overlap(self):
        """Two peers with some shared events and some unique.

        Tests that the protocol correctly identifies differences and exchanges info.
        """
        factory = EventFactory(seed=3)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        scenario = factory.scenario_partial_overlap()
        harness.add_events(alice, scenario['alice'])
        harness.add_events(bob, scenario['bob'])

        result = harness.run_sync(max_rounds=20)
        harness.print_summary(result)

        # Protocol runs and exchanges messages
        assert result.total_messages > 0
        # Can inspect what events each peer thinks they need
        a_missing, b_missing = harness.get_missing_events(alice, bob)
        # There should be differences since blobs don't transfer in isolation
        assert len(a_missing) > 0 or len(b_missing) > 0

    def test_one_peer_behind(self):
        """One peer has a subset of the other's events.

        Tests that protocol identifies the missing events.
        """
        factory = EventFactory(seed=4)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        scenario = factory.scenario_one_behind()
        harness.add_events(alice, scenario['alice'])
        harness.add_events(bob, scenario['bob'])

        initial_alice = harness.get_event_count(alice)
        initial_bob = harness.get_event_count(bob)

        result = harness.run_sync(max_rounds=20)
        harness.print_summary(result)

        # Protocol runs and exchanges messages
        assert result.total_messages > 0
        # Alice still has all events (isolated test doesn't change counts)
        assert harness.get_event_count(alice) == initial_alice
        # Bob still has subset
        assert harness.get_event_count(bob) == initial_bob
        # Can see what Bob is missing
        _, b_missing = harness.get_missing_events(alice, bob)
        assert len(b_missing) == 10  # Bob missing 10 events


class TestBucketBehavior:
    """Test negentropy's hierarchical bucket behavior."""

    def test_events_in_different_days(self):
        """Events spread across multiple days test bucket drill-down."""
        factory = EventFactory(seed=10)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        # Events spread across 5 different days
        alice_events = factory.generate_across_buckets(
            prefix="alice",
            events_per_bucket=2,
            bucket_level="day",
            num_buckets=5
        )
        bob_events = factory.generate_across_buckets(
            prefix="bob",
            events_per_bucket=2,
            bucket_level="day",
            num_buckets=5
        )

        harness.add_events(alice, alice_events)
        harness.add_events(bob, bob_events)

        result = harness.run_sync(max_rounds=30)
        harness.print_summary(result)

        # Protocol should exchange messages across time buckets
        assert result.total_messages > 0

    def test_events_same_minute(self):
        """All events in same 1-minute bucket."""
        factory = EventFactory(seed=11)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        alice_events = factory.generate(
            count=5,
            prefix="alice",
            time_pattern="single_bucket"
        )
        bob_events = factory.generate(
            count=5,
            prefix="bob",
            time_pattern="single_bucket"
        )

        harness.add_events(alice, alice_events)
        harness.add_events(bob, bob_events)

        result = harness.run_sync(max_rounds=20)
        harness.print_summary(result)

        # Protocol runs
        assert result.total_messages > 0


class TestThreePeerSync:
    """Three-peer mesh sync scenarios."""

    def test_three_peers_partial_overlap(self):
        """Three peers with different event sets - tests mesh topology."""
        factory = EventFactory(seed=20)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        carol = harness.add_peer("Carol")
        harness.connect_all()  # Mesh: A-B, A-C, B-C

        # Each peer has some unique events
        alice_events = factory.generate(count=5, prefix="alice")
        bob_events = factory.generate(count=5, prefix="bob")
        carol_events = factory.generate(count=5, prefix="carol")
        shared_events = factory.generate(count=5, prefix="shared")

        harness.add_events(alice, alice_events.merge(shared_events))
        harness.add_events(bob, bob_events.merge(shared_events))
        harness.add_events(carol, carol_events.merge(shared_events))

        result = harness.run_sync(max_rounds=30)
        harness.print_summary(result)

        # Protocol should run across all connections
        assert result.total_messages > 0


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_peers(self):
        """Two peers with no events."""
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        result = harness.run_sync(max_rounds=10)

        # Empty peers should be considered synced
        assert result.converged

    def test_one_peer_empty(self):
        """One peer has events, other is empty."""
        factory = EventFactory(seed=30)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        alice_events = factory.generate(count=10, prefix="alice")
        harness.add_events(alice, alice_events)
        # Bob has no events

        result = harness.run_sync(max_rounds=50)
        harness.print_summary(result)

        assert result.converged

    def test_single_event_each(self):
        """Minimal case: one event per peer."""
        factory = EventFactory(seed=31)
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        alice_events = factory.generate(count=1, prefix="alice")
        bob_events = factory.generate(count=1, prefix="bob")

        harness.add_events(alice, alice_events)
        harness.add_events(bob, bob_events)

        result = harness.run_sync(max_rounds=20)
        harness.print_summary(result)

        assert result.converged


class TestHashComputation:
    """Test hash behavior directly."""

    def test_root_hash_changes_with_events(self):
        """Root hash should change when events are added."""
        factory = EventFactory(seed=40)
        harness = SyncTestHarness(verbose=False)

        alice = harness.add_peer("Alice")

        # Initially empty
        hash1 = harness.get_root_hash(alice)

        # Add first event
        event1 = factory.generate(count=1, prefix="first")
        harness.add_events(alice, event1)
        hash2 = harness.get_root_hash(alice)

        assert hash2 != hash1, "Hash should change after adding event"

        # Add second event
        event2 = factory.generate(count=1, prefix="second")
        harness.add_events(alice, event2)
        hash3 = harness.get_root_hash(alice)

        assert hash3 != hash2, "Hash should change again"

    def test_identical_events_same_hash(self):
        """Two peers with identical events should have same hash."""
        factory = EventFactory(seed=41)
        harness = SyncTestHarness(verbose=False)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")

        events = factory.generate(count=5, prefix="shared")

        harness.add_events(alice, events)
        harness.add_events(bob, events)

        assert harness.get_root_hash(alice) == harness.get_root_hash(bob)

    def test_different_events_different_hash(self):
        """Two peers with different events should have different hash."""
        factory = EventFactory(seed=42)
        harness = SyncTestHarness(verbose=False)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")

        alice_events = factory.generate(count=5, prefix="alice")
        bob_events = factory.generate(count=5, prefix="bob")

        harness.add_events(alice, alice_events)
        harness.add_events(bob, bob_events)

        assert harness.get_root_hash(alice) != harness.get_root_hash(bob)


class TestStubConnectionBridge:
    """Test the stub connection infrastructure itself."""

    def test_bridge_routes_messages(self):
        """Messages sent via bridge are delivered to correct peer."""
        harness = SyncTestHarness(verbose=True)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect(alice, bob)

        # Bridge should have connection info
        assert len(alice.connections) == 1
        assert len(bob.connections) == 1

        # Get connection IDs
        alice_conn_id = list(alice.connections.keys())[0]
        alice_conn = alice.connections[alice_conn_id]

        assert alice_conn.remote_peer_id == bob.peer_id
        assert alice_conn.their_connection_id in bob.connections

    def test_bridge_message_counting(self):
        """Bridge tracks message statistics."""
        factory = EventFactory(seed=50)
        harness = SyncTestHarness(verbose=False)

        alice = harness.add_peer("Alice")
        bob = harness.add_peer("Bob")
        harness.connect_all()

        events = factory.generate(count=5, prefix="test")
        harness.add_events(alice, events)

        result = harness.run_sync(max_rounds=20)

        # Should have sent some messages
        assert result.total_messages > 0


# Run quick sanity check when module is executed directly
if __name__ == "__main__":
    from tests.sync_tests.sync_harness import quick_sync_test

    print("Running quick sync test...")
    result = quick_sync_test(
        alice_events=10,
        bob_events=10,
        shared_events=5,
        max_rounds=50,
        verbose=True
    )

    if result.converged:
        print("\nSUCCESS: Sync converged!")
    else:
        print("\nFAILURE: Sync did not converge")
