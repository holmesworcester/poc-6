"""Sync test harness - orchestrates isolated sync testing with rich visibility.

The harness:
1. Sets up peers with stub connections
2. Distributes events according to test scenario
3. Runs sync rounds with detailed logging
4. Validates convergence
5. Reports sync statistics

Usage:
    harness = SyncTestHarness(verbose=True)
    alice = harness.add_peer("Alice")
    bob = harness.add_peer("Bob")
    harness.connect_all()

    # Add events
    harness.add_events(alice, alice_events)
    harness.add_events(bob, bob_events)

    # Run sync
    result = harness.run_sync(max_rounds=50)

    # Check results
    assert result.converged
    assert harness.peers_have_same_events()
"""

import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

from db import create_safe_db
from events.network import negentropy

from .stub_connection import StubConnectionBridge, StubPeer, StubConnection
from .event_factory import EventSet, TestEvent

log = logging.getLogger(__name__)


@dataclass
class SyncRoundStats:
    """Statistics for a single sync round."""
    round_num: int
    t_ms: int
    messages_sent: int
    messages_delivered: int
    events_by_peer: dict[str, int]  # peer_name -> event count


@dataclass
class SyncResult:
    """Result of a sync test run."""
    converged: bool
    rounds_taken: int
    total_messages: int
    final_event_counts: dict[str, int]  # peer_name -> final count
    expected_count: Optional[int] = None
    round_stats: list[SyncRoundStats] = field(default_factory=list)
    error: Optional[str] = None


class SyncTestHarness:
    """Harness for isolated sync protocol testing.

    Provides a controlled environment for testing sync protocols:
    - Stubbed connections (no encryption/handshake overhead)
    - Direct event injection
    - Rich logging of protocol state
    - Convergence detection
    """

    def __init__(self, verbose: bool = True, log_level: int = logging.INFO):
        self.bridge = StubConnectionBridge(verbose=verbose)
        self.verbose = verbose
        self.t_ms = 1000  # Current logical time
        self.round_stats: list[SyncRoundStats] = []

        # Set up logging
        if verbose:
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(log_level)
            formatter = logging.Formatter(
                '%(message)s'
            )
            handler.setFormatter(formatter)

            # Add handler to our logger and stub logger
            for logger_name in ['tests.sync_tests', __name__]:
                logger = logging.getLogger(logger_name)
                logger.addHandler(handler)
                logger.setLevel(log_level)

            # Also enable negentropy logging
            negentropy_logger = logging.getLogger('events.network.negentropy')
            negentropy_logger.addHandler(handler)
            negentropy_logger.setLevel(log_level)

    def add_peer(self, name: str) -> StubPeer:
        """Add a peer to the test network."""
        peer = self.bridge.add_peer(name, self.t_ms)
        self.t_ms += 100
        return peer

    def connect(self, peer_a: StubPeer, peer_b: StubPeer) -> None:
        """Create bidirectional connection between two peers."""
        self.bridge.connect(peer_a, peer_b, self.t_ms)
        self.t_ms += 100

    def connect_all(self) -> None:
        """Create mesh connections between all peers."""
        peers = list(self.bridge.peers.values())
        for i, peer_a in enumerate(peers):
            for peer_b in peers[i+1:]:
                self.connect(peer_a, peer_b)

    def add_events(self, peer: StubPeer, events: EventSet) -> int:
        """Add events to a peer's sync system.

        Events are added directly to negentropy tracking.
        Returns number of events added.
        """
        count = 0
        for event in events:
            negentropy.add_event_to_sync(
                peer.db,
                peer.peer_id,
                event.event_id,
                event.created_at
            )
            count += 1
        peer.db.commit()

        if self.verbose:
            log.info(f"[HARNESS] Added {count} events to {peer.name}")

        return count

    def add_event(self, peer: StubPeer, event: TestEvent) -> None:
        """Add a single event to a peer."""
        negentropy.add_event_to_sync(
            peer.db,
            peer.peer_id,
            event.event_id,
            event.created_at
        )
        peer.db.commit()

    def get_event_count(self, peer: StubPeer) -> int:
        """Get number of events in peer's negentropy system."""
        return negentropy.get_total_event_count(peer.db, peer.peer_id)

    def get_event_ids(self, peer: StubPeer) -> set[str]:
        """Get all event IDs in peer's negentropy system."""
        safedb = create_safe_db(peer.db, recorded_by=peer.peer_id)
        rows = safedb.query(
            "SELECT event_id FROM negentropy_events WHERE recorded_by = ?",
            (peer.peer_id,)
        )
        return {row['event_id'] for row in rows}

    def get_root_hash(self, peer: StubPeer) -> bytes:
        """Get peer's current root hash."""
        return negentropy.get_root_hash(peer.db, peer.peer_id)

    def peers_have_same_events(self) -> bool:
        """Check if all peers have identical event sets."""
        if len(self.bridge.peers) < 2:
            return True

        peers = list(self.bridge.peers.values())
        first_events = self.get_event_ids(peers[0])

        for peer in peers[1:]:
            if self.get_event_ids(peer) != first_events:
                return False

        return True

    def peers_have_same_hash(self) -> bool:
        """Check if all peers have identical root hash."""
        if len(self.bridge.peers) < 2:
            return True

        peers = list(self.bridge.peers.values())
        first_hash = self.get_root_hash(peers[0])

        for peer in peers[1:]:
            if self.get_root_hash(peer) != first_hash:
                return False

        return True

    def get_missing_events(self, peer_a: StubPeer, peer_b: StubPeer) -> tuple[set[str], set[str]]:
        """Get events that each peer is missing from the other.

        Returns (a_missing, b_missing) where:
        - a_missing: events B has that A doesn't
        - b_missing: events A has that B doesn't
        """
        a_events = self.get_event_ids(peer_a)
        b_events = self.get_event_ids(peer_b)

        return (b_events - a_events, a_events - b_events)

    def _run_sync_round(self) -> SyncRoundStats:
        """Run one round of sync for all peers.

        A round consists of:
        1. Each peer initiates sync on all connections
        2. Deliver all messages
        3. Process incoming messages (handle negentropy protocol)
        """
        round_num = len(self.round_stats) + 1
        messages_sent = 0

        if self.verbose:
            log.info(f"\n=== Round {round_num} (t={self.t_ms}) ===")

        # Install stub bridge if not already
        self.bridge.install()

        # Each peer initiates/continues sync
        for peer in self.bridge.peers.values():
            for conn_id, conn in peer.connections.items():
                # Create a Connection-like object for negentropy
                sent = negentropy.sync_connection(
                    peer.db,
                    peer.peer_id,
                    conn,
                    self.t_ms
                )
                messages_sent += sent

        # Deliver messages and process responses
        delivered = self.bridge.deliver_all(self.t_ms)

        # Process incoming messages on each peer
        for peer in self.bridge.peers.values():
            self._process_incoming(peer)

        # Collect stats
        event_counts = {
            peer.name: self.get_event_count(peer)
            for peer in self.bridge.peers.values()
        }

        stats = SyncRoundStats(
            round_num=round_num,
            t_ms=self.t_ms,
            messages_sent=messages_sent,
            messages_delivered=delivered,
            events_by_peer=event_counts,
        )
        self.round_stats.append(stats)

        if self.verbose:
            log.info(f"  Sent: {messages_sent}, Delivered: {delivered}")
            for name, count in event_counts.items():
                hash_val = self.get_root_hash(
                    next(p for p in self.bridge.peers.values() if p.name == name)
                )
                log.info(f"  {name}: {count} events, hash={hash_val.hex()[:16] if hash_val else 'empty'}...")

        self.t_ms += 100
        return stats

    def _process_incoming(self, peer: StubPeer) -> int:
        """Process incoming negentropy messages for a peer.

        Reads from incoming_blobs and handles negentropy envelopes.
        """
        import crypto

        processed = 0
        safedb = create_safe_db(peer.db, recorded_by=peer.peer_id)

        # Get all pending incoming blobs (use sent_at as column name per schema)
        # The table has id as primary key
        rows = peer.db.query("SELECT id, blob FROM incoming_blobs WHERE dropped = 0 AND deliver_at <= ?", (self.t_ms,))

        for row in rows:
            blob_id = row['id']
            blob = row['blob']

            try:
                data = crypto.parse_json(blob)

                if data.get('type') == 'negentropy':
                    # Handle negentropy message
                    # Use reply_connection_id as our connection_id
                    connection_id = data.get('reply_connection_id')
                    if connection_id:
                        negentropy.handle_incoming(
                            peer.db,
                            peer.peer_id,
                            connection_id,
                            data,
                            self.t_ms
                        )
                        processed += 1

                # Remove processed blob
                peer.db.execute("DELETE FROM incoming_blobs WHERE id = ?", (blob_id,))

            except Exception as e:
                if self.verbose:
                    log.warning(f"  Error processing blob for {peer.name}: {e}")
                # Remove bad blob anyway
                peer.db.execute("DELETE FROM incoming_blobs WHERE id = ?", (blob_id,))

        peer.db.commit()
        return processed

    def run_sync(
        self,
        max_rounds: int = 100,
        expected_count: Optional[int] = None,
        convergence_threshold: int = 5
    ) -> SyncResult:
        """Run sync until convergence or max_rounds.

        Convergence is detected when:
        1. All peers have the same root hash
        2. Message activity has stopped for convergence_threshold rounds

        Args:
            max_rounds: Maximum sync rounds to run
            expected_count: Expected final event count (for validation)
            convergence_threshold: Rounds without change to consider converged

        Returns:
            SyncResult with convergence status and statistics
        """
        if self.verbose:
            log.info(f"\n{'='*60}")
            log.info(f"Starting sync test with {len(self.bridge.peers)} peers")
            log.info(f"{'='*60}")

        # Install bridge
        self.bridge.install()

        try:
            stable_rounds = 0
            prev_hashes = {}

            for _ in range(max_rounds):
                stats = self._run_sync_round()

                # Check for convergence
                current_hashes = {
                    peer.name: self.get_root_hash(peer).hex() if self.get_root_hash(peer) else ''
                    for peer in self.bridge.peers.values()
                }

                # Are all hashes the same?
                unique_hashes = set(current_hashes.values())
                all_same = len(unique_hashes) == 1 and '' not in unique_hashes

                # Has anything changed?
                if current_hashes == prev_hashes:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                prev_hashes = current_hashes

                # Converged if same hashes and stable
                if all_same and stable_rounds >= convergence_threshold:
                    if self.verbose:
                        log.info(f"\n*** Converged after {stats.round_num} rounds ***")

                    final_counts = {
                        peer.name: self.get_event_count(peer)
                        for peer in self.bridge.peers.values()
                    }

                    return SyncResult(
                        converged=True,
                        rounds_taken=stats.round_num,
                        total_messages=sum(s.messages_sent for s in self.round_stats),
                        final_event_counts=final_counts,
                        expected_count=expected_count,
                        round_stats=self.round_stats,
                    )

            # Did not converge
            if self.verbose:
                log.info(f"\n*** Did not converge within {max_rounds} rounds ***")

            final_counts = {
                peer.name: self.get_event_count(peer)
                for peer in self.bridge.peers.values()
            }

            return SyncResult(
                converged=False,
                rounds_taken=max_rounds,
                total_messages=sum(s.messages_sent for s in self.round_stats),
                final_event_counts=final_counts,
                expected_count=expected_count,
                round_stats=self.round_stats,
                error="Did not converge within max_rounds"
            )

        finally:
            self.bridge.uninstall()

    def print_summary(self, result: SyncResult) -> None:
        """Print a summary of sync test results."""
        print(f"\n{'='*60}")
        print("SYNC TEST SUMMARY")
        print(f"{'='*60}")
        print(f"Converged: {result.converged}")
        print(f"Rounds: {result.rounds_taken}")
        print(f"Total messages: {result.total_messages}")
        print()
        print("Final event counts:")
        for name, count in result.final_event_counts.items():
            marker = ""
            if result.expected_count is not None:
                if count == result.expected_count:
                    marker = " [OK]"
                else:
                    marker = f" [EXPECTED {result.expected_count}]"
            print(f"  {name}: {count}{marker}")

        if not result.converged:
            print()
            print(f"ERROR: {result.error}")

        # Show peer differences if not converged
        if not self.peers_have_same_events():
            print()
            print("Event differences:")
            peers = list(self.bridge.peers.values())
            for i, peer_a in enumerate(peers):
                for peer_b in peers[i+1:]:
                    a_missing, b_missing = self.get_missing_events(peer_a, peer_b)
                    if a_missing or b_missing:
                        print(f"  {peer_a.name} vs {peer_b.name}:")
                        if a_missing:
                            print(f"    {peer_a.name} missing: {len(a_missing)} events")
                        if b_missing:
                            print(f"    {peer_b.name} missing: {len(b_missing)} events")

    def reset(self) -> None:
        """Reset harness state for a new test."""
        self.bridge = StubConnectionBridge(verbose=self.verbose)
        self.t_ms = 1000
        self.round_stats.clear()


# Convenience function for quick tests
def quick_sync_test(
    alice_events: int = 10,
    bob_events: int = 10,
    shared_events: int = 5,
    max_rounds: int = 50,
    verbose: bool = True
) -> SyncResult:
    """Run a quick sync test with generated events.

    Convenience function for simple two-peer sync tests.
    """
    from .event_factory import EventFactory

    factory = EventFactory()
    harness = SyncTestHarness(verbose=verbose)

    alice = harness.add_peer("Alice")
    bob = harness.add_peer("Bob")
    harness.connect_all()

    alice_only, bob_only, shared = factory.partition(
        alice_only=alice_events,
        bob_only=bob_events,
        shared=shared_events
    )

    harness.add_events(alice, alice_only.merge(shared))
    harness.add_events(bob, bob_only.merge(shared))

    expected = alice_events + bob_events + shared_events
    result = harness.run_sync(max_rounds=max_rounds, expected_count=expected)

    if verbose:
        harness.print_summary(result)

    return result
