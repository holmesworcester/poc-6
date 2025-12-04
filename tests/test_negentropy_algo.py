"""Prototype: Test negentropy O(log n) algorithm in isolation.

This tests the core algorithm with stubbed connection.send() and generated test data.
"""
import sqlite3
import hashlib
import random
import string
from dataclasses import dataclass
from typing import Optional


# =============================================================================
# Schema
# =============================================================================

SCHEMA = """
-- Leaf items (source of truth)
CREATE TABLE negentropy_items (
    recorded_by TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    item_hash BLOB NOT NULL,
    PRIMARY KEY (recorded_by, timestamp_ms, event_id)
);

-- Hierarchical buckets (virtual segment tree)
CREATE TABLE negentropy_buckets (
    recorded_by TEXT NOT NULL,
    level INTEGER NOT NULL,
    bucket_id INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    start_id TEXT NOT NULL,
    end_ts INTEGER NOT NULL,
    end_id TEXT NOT NULL,
    xor_hash BLOB NOT NULL,
    item_count INTEGER NOT NULL,
    PRIMARY KEY (recorded_by, level, bucket_id)
);

CREATE INDEX idx_buckets_range
ON negentropy_buckets(recorded_by, level, start_ts, start_id);

CREATE INDEX idx_items_range
ON negentropy_items(recorded_by, timestamp_ms, event_id);
"""


# =============================================================================
# Helpers
# =============================================================================

def xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR two byte strings of equal length."""
    return bytes(x ^ y for x, y in zip(a, b))


def hash_event_id(event_id: str) -> bytes:
    """Hash an event_id to 16 bytes."""
    return hashlib.blake2b(event_id.encode(), digest_size=16).digest()


def generate_event_id() -> str:
    """Generate a random event_id (simulating real event hashes)."""
    return ''.join(random.choices(string.hexdigits.lower(), k=64))


ZERO_HASH = bytes(16)


# =============================================================================
# Core Algorithm
# =============================================================================

class NegentropyStore:
    """O(log n) negentropy store using hierarchical buckets."""

    # Bucket sizing: level 0 = 16 items, level 1 = 256, level 2 = 4096, etc.
    BUCKET_SIZE_BITS = 4  # 2^4 = 16 items per bucket at level 0
    MAX_LEVELS = 8  # Supports up to 16^8 = 4 billion items

    # Threshold for sending IDs vs fingerprints
    ID_THRESHOLD = 100

    def __init__(self, db: sqlite3.Connection, recorded_by: str):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.recorded_by = recorded_by
        self._item_count = 0
        self._sorted_items: list[tuple[int, str]] = []  # Cache for simple impl

    def insert(self, timestamp_ms: int, event_id: str) -> None:
        """Insert an item. O(log n) with bucket updates."""
        item_hash = hash_event_id(event_id)

        # Insert into items table
        self.db.execute("""
            INSERT OR IGNORE INTO negentropy_items
            (recorded_by, timestamp_ms, event_id, item_hash)
            VALUES (?, ?, ?, ?)
        """, (self.recorded_by, timestamp_ms, event_id, item_hash))

        # Update cache
        self._sorted_items.append((timestamp_ms, event_id))
        self._sorted_items.sort()
        self._item_count += 1

        # For prototype, we'll compute fingerprints on-the-fly
        # Production would update bucket hierarchy here

    def get_root_fingerprint(self) -> bytes:
        """Get fingerprint of all items."""
        return self.range_fingerprint(None, None)

    def range_fingerprint(
        self,
        start: Optional[tuple[int, str]],
        end: Optional[tuple[int, str]]
    ) -> bytes:
        """Compute fingerprint for range [start, end).

        None means unbounded (start=None means from beginning, end=None means to end).

        For prototype: O(n) scan. Production would use bucket hierarchy for O(log n).
        """
        result = ZERO_HASH

        for ts, eid in self._sorted_items:
            key = (ts, eid)
            if start is not None and key < start:
                continue
            if end is not None and key >= end:
                break

            item_hash = hash_event_id(eid)
            result = xor_bytes(result, item_hash)

        return result

    def get_items_in_range(
        self,
        start: Optional[tuple[int, str]],
        end: Optional[tuple[int, str]]
    ) -> list[tuple[int, str]]:
        """Get all items in range [start, end)."""
        result = []
        for ts, eid in self._sorted_items:
            key = (ts, eid)
            if start is not None and key < start:
                continue
            if end is not None and key >= end:
                break
            result.append((ts, eid))
        return result

    def count_in_range(
        self,
        start: Optional[tuple[int, str]],
        end: Optional[tuple[int, str]]
    ) -> int:
        """Count items in range."""
        return len(self.get_items_in_range(start, end))

    def get_split_point(
        self,
        start: Optional[tuple[int, str]],
        end: Optional[tuple[int, str]]
    ) -> Optional[tuple[int, str]]:
        """Find median split point for a range."""
        items = self.get_items_in_range(start, end)
        if len(items) < 2:
            return None
        mid_idx = len(items) // 2
        return items[mid_idx]

    @property
    def total_count(self) -> int:
        return self._item_count


# =============================================================================
# Protocol Messages
# =============================================================================

@dataclass
class RangeFingerprint:
    """A range with its fingerprint."""
    start: Optional[tuple[int, str]]  # None = from beginning
    end: Optional[tuple[int, str]]    # None = to end
    fingerprint: bytes
    count: int


@dataclass
class RangeIdList:
    """A range with explicit item IDs (when count <= threshold)."""
    start: Optional[tuple[int, str]]
    end: Optional[tuple[int, str]]
    items: list[tuple[int, str]]


# =============================================================================
# Protocol Implementation
# =============================================================================

class NegentropySyncer:
    """Handles negentropy sync protocol for one peer."""

    def __init__(self, store: NegentropyStore):
        self.store = store
        self.pending_ranges: list[tuple[Optional[tuple], Optional[tuple]]] = []
        self.items_to_send: list[tuple[int, str]] = []
        self.items_received: list[tuple[int, str]] = []

    def initiate(self) -> list[RangeFingerprint]:
        """Start sync by sending root fingerprint."""
        fp = self.store.get_root_fingerprint()
        count = self.store.total_count
        return [RangeFingerprint(None, None, fp, count)]

    def process_fingerprints(
        self,
        their_ranges: list[RangeFingerprint]
    ) -> tuple[list[RangeFingerprint | RangeIdList], list[tuple[int, str]]]:
        """Process received fingerprints, return our response and items we need."""

        responses = []
        items_we_need = []

        for r in their_ranges:
            our_fp = self.store.range_fingerprint(r.start, r.end)
            our_count = self.store.count_in_range(r.start, r.end)

            if our_fp == r.fingerprint:
                # Match! This range is synced.
                continue

            # Mismatch - decide whether to split or send IDs
            total_count = max(our_count, r.count)

            if total_count <= self.store.ID_THRESHOLD:
                # Small enough - send our IDs for this range
                our_items = self.store.get_items_in_range(r.start, r.end)
                responses.append(RangeIdList(r.start, r.end, our_items))
            else:
                # Split the range and send fingerprints for each half
                split = self.store.get_split_point(r.start, r.end)
                if split is None:
                    # Can't split (we have < 2 items), send IDs
                    our_items = self.store.get_items_in_range(r.start, r.end)
                    responses.append(RangeIdList(r.start, r.end, our_items))
                else:
                    # Send fingerprints for both halves
                    left_fp = self.store.range_fingerprint(r.start, split)
                    left_count = self.store.count_in_range(r.start, split)
                    responses.append(RangeFingerprint(r.start, split, left_fp, left_count))

                    right_fp = self.store.range_fingerprint(split, r.end)
                    right_count = self.store.count_in_range(split, r.end)
                    responses.append(RangeFingerprint(split, r.end, right_fp, right_count))

        return responses, items_we_need

    def process_id_list(
        self,
        their_list: RangeIdList
    ) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """Process received ID list. Return (items_to_send, items_we_need)."""

        our_items = set(self.store.get_items_in_range(their_list.start, their_list.end))
        their_items = set(their_list.items)

        items_to_send = list(our_items - their_items)  # We have, they don't
        items_we_need = list(their_items - our_items)  # They have, we don't

        return items_to_send, items_we_need


# =============================================================================
# Stub: connection.send() and receive
# =============================================================================

def stub_connection_send(msg, conn_id: str) -> None:
    """Stub for connection.send() - just prints for now."""
    pass  # In real impl, this creates an event and sends over connection


def stub_receive(msg):
    """Stub for receiving - in real impl this comes from projection."""
    pass


# =============================================================================
# Test Harness
# =============================================================================

def create_test_db() -> sqlite3.Connection:
    """Create in-memory test database."""
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    return db


def run_sync_simulation(
    alice_store: NegentropyStore,
    bob_store: NegentropyStore,
    max_rounds: int = 20
) -> dict:
    """Simulate sync between two peers. Returns stats."""

    alice_syncer = NegentropySyncer(alice_store)
    bob_syncer = NegentropySyncer(bob_store)

    # Track what needs to be exchanged
    alice_needs: set[tuple[int, str]] = set()
    bob_needs: set[tuple[int, str]] = set()

    # Round 1: Alice initiates
    messages = alice_syncer.initiate()
    rounds = 1
    total_messages = 1

    while messages and rounds < max_rounds:
        rounds += 1
        next_messages = []

        for msg in messages:
            if isinstance(msg, RangeFingerprint):
                # Bob processes fingerprint
                responses, _ = bob_syncer.process_fingerprints([msg])
                next_messages.extend(responses)
                total_messages += len(responses)

            elif isinstance(msg, RangeIdList):
                # Bob processes ID list
                to_send, needs = bob_syncer.process_id_list(msg)
                bob_needs.update(needs)

                # Bob sends back his IDs for this range
                bob_items = bob_store.get_items_in_range(msg.start, msg.end)
                if bob_items:
                    # Alice will process Bob's response
                    alice_to_send, alice_to_recv = alice_syncer.process_id_list(
                        RangeIdList(msg.start, msg.end, bob_items)
                    )
                    alice_needs.update(alice_to_recv)
                    total_messages += 1

        # Swap roles - now process responses
        if next_messages:
            # Alice processes Bob's responses
            further_messages = []
            for msg in next_messages:
                if isinstance(msg, RangeFingerprint):
                    responses, _ = alice_syncer.process_fingerprints([msg])
                    further_messages.extend(responses)
                    total_messages += len(responses)
                elif isinstance(msg, RangeIdList):
                    to_send, needs = alice_syncer.process_id_list(msg)
                    alice_needs.update(needs)

                    # Send our items for this range back
                    our_items = alice_store.get_items_in_range(msg.start, msg.end)
                    if our_items:
                        bob_to_send, bob_to_recv = bob_syncer.process_id_list(
                            RangeIdList(msg.start, msg.end, our_items)
                        )
                        bob_needs.update(bob_to_recv)
                        total_messages += 1

            messages = further_messages
        else:
            messages = []

    return {
        'rounds': rounds,
        'total_messages': total_messages,
        'alice_needs': len(alice_needs),
        'bob_needs': len(bob_needs),
        'alice_needs_items': alice_needs,
        'bob_needs_items': bob_needs,
    }


# =============================================================================
# Tests
# =============================================================================

def test_identical_sets():
    """Two peers with identical sets should sync in 1 round."""
    print("\n=== Test: Identical Sets ===")

    db = create_test_db()
    alice = NegentropyStore(db, "alice")
    bob = NegentropyStore(db, "bob")

    # Add same items to both
    for i in range(100):
        ts = 1000000 + i * 1000
        eid = f"event_{i:04d}"
        alice.insert(ts, eid)
        bob.insert(ts, eid)

    # Verify fingerprints match
    assert alice.get_root_fingerprint() == bob.get_root_fingerprint()

    stats = run_sync_simulation(alice, bob)
    print(f"  Rounds: {stats['rounds']}")
    print(f"  Messages: {stats['total_messages']}")
    print(f"  Alice needs: {stats['alice_needs']}, Bob needs: {stats['bob_needs']}")

    assert stats['alice_needs'] == 0
    assert stats['bob_needs'] == 0
    print("  ✓ PASS")


def test_disjoint_sets():
    """Two peers with completely different sets."""
    print("\n=== Test: Disjoint Sets ===")

    db = create_test_db()
    alice = NegentropyStore(db, "alice")
    bob = NegentropyStore(db, "bob")

    # Alice has events 0-49
    for i in range(50):
        ts = 1000000 + i * 1000
        eid = f"alice_event_{i:04d}"
        alice.insert(ts, eid)

    # Bob has events 50-99
    for i in range(50, 100):
        ts = 1000000 + i * 1000
        eid = f"bob_event_{i:04d}"
        bob.insert(ts, eid)

    stats = run_sync_simulation(alice, bob)
    print(f"  Rounds: {stats['rounds']}")
    print(f"  Messages: {stats['total_messages']}")
    print(f"  Alice needs: {stats['alice_needs']}, Bob needs: {stats['bob_needs']}")

    assert stats['alice_needs'] == 50  # Alice needs Bob's 50 events
    assert stats['bob_needs'] == 50    # Bob needs Alice's 50 events
    print("  ✓ PASS")


def test_mostly_overlapping():
    """Two peers with mostly overlapping sets, a few differences."""
    print("\n=== Test: Mostly Overlapping (realistic) ===")

    db = create_test_db()
    alice = NegentropyStore(db, "alice")
    bob = NegentropyStore(db, "bob")

    # Both have events 0-999 (shared)
    for i in range(1000):
        ts = 1000000 + i * 1000
        eid = f"shared_event_{i:04d}"
        alice.insert(ts, eid)
        bob.insert(ts, eid)

    # Alice has 5 extra events Bob doesn't have
    for i in range(5):
        ts = 2000000 + i * 1000
        eid = f"alice_only_{i:04d}"
        alice.insert(ts, eid)

    # Bob has 3 extra events Alice doesn't have
    for i in range(3):
        ts = 3000000 + i * 1000
        eid = f"bob_only_{i:04d}"
        bob.insert(ts, eid)

    stats = run_sync_simulation(alice, bob)
    print(f"  Rounds: {stats['rounds']}")
    print(f"  Messages: {stats['total_messages']}")
    print(f"  Alice needs: {stats['alice_needs']}, Bob needs: {stats['bob_needs']}")

    assert stats['alice_needs'] == 3  # Alice needs Bob's 3 events
    assert stats['bob_needs'] == 5    # Bob needs Alice's 5 events
    print("  ✓ PASS")


def test_one_empty():
    """One peer is empty, other has data."""
    print("\n=== Test: One Empty Peer ===")

    db = create_test_db()
    alice = NegentropyStore(db, "alice")
    bob = NegentropyStore(db, "bob")

    # Only Alice has events
    for i in range(50):
        ts = 1000000 + i * 1000
        eid = f"alice_event_{i:04d}"
        alice.insert(ts, eid)

    stats = run_sync_simulation(alice, bob)
    print(f"  Rounds: {stats['rounds']}")
    print(f"  Messages: {stats['total_messages']}")
    print(f"  Alice needs: {stats['alice_needs']}, Bob needs: {stats['bob_needs']}")

    assert stats['alice_needs'] == 0   # Alice has everything
    assert stats['bob_needs'] == 50    # Bob needs all of Alice's events
    print("  ✓ PASS")


def test_same_timestamp_different_ids():
    """Many events with same timestamp (file upload scenario)."""
    print("\n=== Test: Same Timestamp, Different IDs ===")

    db = create_test_db()
    alice = NegentropyStore(db, "alice")
    bob = NegentropyStore(db, "bob")

    # All events at same timestamp!
    ts = 1000000

    # Both have 100 shared events
    for i in range(100):
        eid = f"shared_{i:04d}"
        alice.insert(ts, eid)
        bob.insert(ts, eid)

    # Alice has 10 extra
    for i in range(10):
        eid = f"alice_only_{i:04d}"
        alice.insert(ts, eid)

    # Bob has 5 extra
    for i in range(5):
        eid = f"bob_only_{i:04d}"
        bob.insert(ts, eid)

    stats = run_sync_simulation(alice, bob)
    print(f"  Rounds: {stats['rounds']}")
    print(f"  Messages: {stats['total_messages']}")
    print(f"  Alice needs: {stats['alice_needs']}, Bob needs: {stats['bob_needs']}")

    assert stats['alice_needs'] == 5   # Alice needs Bob's 5 events
    assert stats['bob_needs'] == 10    # Bob needs Alice's 10 events
    print("  ✓ PASS")


def test_large_scale():
    """Large scale test with 10k events."""
    print("\n=== Test: Large Scale (10k events) ===")

    db = create_test_db()
    alice = NegentropyStore(db, "alice")
    bob = NegentropyStore(db, "bob")

    # Both have 10k shared events
    print("  Generating 10k shared events...")
    for i in range(10000):
        ts = 1000000 + i * 100
        eid = f"shared_{i:06d}"
        alice.insert(ts, eid)
        bob.insert(ts, eid)

    # Alice has 20 extra (0.2% difference)
    for i in range(20):
        ts = 2000000 + i * 1000
        eid = f"alice_only_{i:04d}"
        alice.insert(ts, eid)

    # Bob has 15 extra
    for i in range(15):
        ts = 3000000 + i * 1000
        eid = f"bob_only_{i:04d}"
        bob.insert(ts, eid)

    print("  Running sync...")
    stats = run_sync_simulation(alice, bob)
    print(f"  Rounds: {stats['rounds']}")
    print(f"  Messages: {stats['total_messages']}")
    print(f"  Alice needs: {stats['alice_needs']}, Bob needs: {stats['bob_needs']}")

    assert stats['alice_needs'] == 15
    assert stats['bob_needs'] == 20
    print("  ✓ PASS")


def test_xor_properties():
    """Verify XOR fingerprint properties."""
    print("\n=== Test: XOR Properties ===")

    # XOR is commutative and associative
    a = hash_event_id("event_a")
    b = hash_event_id("event_b")
    c = hash_event_id("event_c")

    # Order doesn't matter
    assert xor_bytes(xor_bytes(a, b), c) == xor_bytes(a, xor_bytes(b, c))
    assert xor_bytes(a, b) == xor_bytes(b, a)

    # Self-inverse
    assert xor_bytes(a, a) == ZERO_HASH

    # Combining and splitting
    ab = xor_bytes(a, b)
    abc = xor_bytes(ab, c)
    bc = xor_bytes(b, c)
    assert xor_bytes(a, bc) == abc

    print("  ✓ PASS")


if __name__ == "__main__":
    test_xor_properties()
    test_identical_sets()
    test_disjoint_sets()
    test_mostly_overlapping()
    test_one_empty()
    test_same_timestamp_different_ids()
    test_large_scale()

    print("\n" + "="*50)
    print("All tests passed!")
