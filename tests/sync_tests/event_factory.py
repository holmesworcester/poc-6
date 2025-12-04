"""Event factory for generating test data in sync tests.

Generates realistic event IDs and timestamps for testing sync protocols.
Content is irrelevant for sync - only IDs and timestamps matter.

Features:
- Generate deterministic event sets for reproducibility
- Partition events between peers (Alice-only, Bob-only, shared)
- Distribute events across time ranges (for negentropy bucket testing)
- Simple API for common test scenarios
"""

import hashlib
import random
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class TestEvent:
    """A minimal test event - just ID and timestamp."""
    event_id: str
    created_at: int  # milliseconds

    def __hash__(self):
        return hash(self.event_id)

    def __eq__(self, other):
        if isinstance(other, TestEvent):
            return self.event_id == other.event_id
        return False


@dataclass
class EventSet:
    """A named set of events for a peer."""
    name: str
    events: list[TestEvent] = field(default_factory=list)

    def __len__(self):
        return len(self.events)

    def __iter__(self) -> Iterator[TestEvent]:
        return iter(self.events)

    @property
    def event_ids(self) -> set[str]:
        return {e.event_id for e in self.events}

    def add(self, event: TestEvent) -> None:
        self.events.append(event)

    def merge(self, other: 'EventSet') -> 'EventSet':
        """Create new set with all events from both sets."""
        merged = EventSet(name=f"{self.name}+{other.name}")
        seen = set()
        for e in self.events + other.events:
            if e.event_id not in seen:
                merged.add(e)
                seen.add(e.event_id)
        return merged


class EventFactory:
    """Factory for generating test events.

    Events are generated with deterministic IDs based on a seed,
    making tests reproducible.

    Usage:
        factory = EventFactory(seed=42)

        # Generate events for different peers
        alice_events = factory.generate(count=10, prefix="alice", start_ms=1000)
        bob_events = factory.generate(count=10, prefix="bob", start_ms=1000)

        # Or use scenarios
        alice, bob, shared = factory.partition(
            total=30,
            alice_only=10,
            bob_only=10,
            shared=10
        )
    """

    # Common timestamp ranges for testing
    YEAR_2024_START = 1704067200000  # 2024-01-01 00:00:00 UTC
    YEAR_2024_JUNE = 1717200000000   # 2024-06-01 00:00:00 UTC
    MINUTE_MS = 60_000
    HOUR_MS = 3_600_000
    DAY_MS = 86_400_000

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.rng = random.Random(seed)
        self.counter = 0

    def reset(self) -> None:
        """Reset factory to initial state."""
        self.rng = random.Random(self.seed)
        self.counter = 0

    def _make_event_id(self, prefix: str, index: int) -> str:
        """Generate a deterministic event ID.

        Uses BLAKE2b-128 like real event IDs, but with test data.
        """
        data = f"{prefix}:{index}:{self.seed}".encode()
        digest = hashlib.blake2b(data, digest_size=16).digest()
        # Return as hex string (32 chars) for readability in logs
        return digest.hex()

    def generate(
        self,
        count: int,
        prefix: str = "event",
        start_ms: int = None,
        spread_ms: int = None,
        time_pattern: str = "sequential"
    ) -> EventSet:
        """Generate a set of events.

        Args:
            count: Number of events to generate
            prefix: Prefix for event IDs (for identification in logs)
            start_ms: Starting timestamp (default: YEAR_2024_JUNE)
            spread_ms: Time spread for events (default: count * MINUTE_MS)
            time_pattern: How to distribute timestamps:
                - "sequential": Events evenly spaced
                - "random": Random times within spread
                - "clustered": Events clustered in bursts
                - "single_bucket": All in same 1-minute bucket

        Returns:
            EventSet with generated events
        """
        if start_ms is None:
            start_ms = self.YEAR_2024_JUNE

        if spread_ms is None:
            spread_ms = count * self.MINUTE_MS

        events = EventSet(name=prefix)

        for i in range(count):
            event_id = self._make_event_id(prefix, self.counter)
            self.counter += 1

            if time_pattern == "sequential":
                # Evenly spaced events
                if count > 1:
                    created_at = start_ms + (i * spread_ms // (count - 1))
                else:
                    created_at = start_ms
            elif time_pattern == "random":
                # Random times within spread
                created_at = start_ms + self.rng.randint(0, spread_ms)
            elif time_pattern == "clustered":
                # Cluster events into groups
                cluster_count = max(1, count // 5)
                cluster_idx = i // max(1, (count // cluster_count))
                cluster_start = start_ms + (cluster_idx * spread_ms // cluster_count)
                created_at = cluster_start + self.rng.randint(0, self.MINUTE_MS)
            elif time_pattern == "single_bucket":
                # All events in same 1-minute bucket
                created_at = start_ms + self.rng.randint(0, 59_999)
            else:
                raise ValueError(f"Unknown time_pattern: {time_pattern}")

            events.add(TestEvent(event_id=event_id, created_at=created_at))

        return events

    def partition(
        self,
        total: int = None,
        alice_only: int = 10,
        bob_only: int = 10,
        shared: int = 10,
        start_ms: int = None,
        spread_ms: int = None
    ) -> tuple[EventSet, EventSet, EventSet]:
        """Generate partitioned event sets for two-peer testing.

        Common pattern: Alice has some exclusive events, Bob has some,
        and both share some events. After sync, both should have all.

        Args:
            total: If set, divide evenly (overrides individual counts)
            alice_only: Events only Alice has
            bob_only: Events only Bob has
            shared: Events both have initially
            start_ms: Starting timestamp
            spread_ms: Time spread

        Returns:
            (alice_events, bob_events, shared_events)
        """
        if total is not None:
            # Divide evenly
            alice_only = total // 3
            bob_only = total // 3
            shared = total - alice_only - bob_only

        if start_ms is None:
            start_ms = self.YEAR_2024_JUNE

        alice_set = self.generate(
            count=alice_only,
            prefix="alice",
            start_ms=start_ms,
            spread_ms=spread_ms
        )

        bob_set = self.generate(
            count=bob_only,
            prefix="bob",
            start_ms=start_ms,
            spread_ms=spread_ms
        )

        shared_set = self.generate(
            count=shared,
            prefix="shared",
            start_ms=start_ms,
            spread_ms=spread_ms
        )

        return alice_set, bob_set, shared_set

    def generate_across_buckets(
        self,
        prefix: str = "bucket",
        events_per_bucket: int = 2,
        bucket_level: str = "day",
        num_buckets: int = 5,
        start_ms: int = None
    ) -> EventSet:
        """Generate events spread across multiple time buckets.

        Useful for testing negentropy's hierarchical hash structure.

        Args:
            prefix: Event ID prefix
            events_per_bucket: Events to put in each bucket
            bucket_level: Bucket granularity (day, hour, ten_min, one_min)
            num_buckets: Number of buckets to create events in
            start_ms: Starting timestamp

        Returns:
            EventSet with events spread across buckets
        """
        if start_ms is None:
            start_ms = self.YEAR_2024_JUNE

        bucket_sizes = {
            'one_min': self.MINUTE_MS,
            'ten_min': 10 * self.MINUTE_MS,
            'hour': self.HOUR_MS,
            'day': self.DAY_MS,
        }
        bucket_ms = bucket_sizes.get(bucket_level, self.DAY_MS)

        events = EventSet(name=f"{prefix}_across_{bucket_level}")

        for bucket_idx in range(num_buckets):
            bucket_start = start_ms + (bucket_idx * bucket_ms)
            for i in range(events_per_bucket):
                event_id = self._make_event_id(f"{prefix}_b{bucket_idx}", self.counter)
                self.counter += 1
                # Random time within bucket
                created_at = bucket_start + self.rng.randint(0, bucket_ms - 1)
                events.add(TestEvent(event_id=event_id, created_at=created_at))

        return events

    # Pre-built scenarios for common test cases
    def scenario_basic_divergence(self) -> dict:
        """Two peers with non-overlapping events.

        Returns dict with 'alice' and 'bob' EventSets.
        """
        return {
            'alice': self.generate(count=5, prefix="alice_basic"),
            'bob': self.generate(count=5, prefix="bob_basic"),
            'expected_total': 10,
            'description': "Basic divergence: 5 events each, no overlap"
        }

    def scenario_partial_overlap(self) -> dict:
        """Two peers with some shared events.

        Returns dict with alice, bob, and shared EventSets.
        """
        alice, bob, shared = self.partition(alice_only=5, bob_only=5, shared=5)
        return {
            'alice': alice.merge(shared),
            'bob': bob.merge(shared),
            'alice_only': alice,
            'bob_only': bob,
            'shared': shared,
            'expected_total': 15,
            'description': "Partial overlap: 5 unique each + 5 shared"
        }

    def scenario_one_behind(self) -> dict:
        """One peer is behind (has subset of other's events).

        Simulates a new device joining.
        """
        # Alice has all events, Bob has only some
        all_events = self.generate(count=20, prefix="all")
        bob_subset = EventSet(name="bob_subset")
        for e in all_events.events[:10]:  # Bob has first 10
            bob_subset.add(e)

        return {
            'alice': all_events,
            'bob': bob_subset,
            'alice_only': EventSet(name="alice_new", events=all_events.events[10:]),
            'expected_total': 20,
            'description': "One behind: Alice has 20, Bob has 10 (subset)"
        }

    def scenario_large_divergence(self, count: int = 100) -> dict:
        """Large number of divergent events for stress testing."""
        alice, bob, shared = self.partition(
            alice_only=count,
            bob_only=count,
            shared=count // 2
        )
        return {
            'alice': alice.merge(shared),
            'bob': bob.merge(shared),
            'alice_only': alice,
            'bob_only': bob,
            'shared': shared,
            'expected_total': count * 2 + count // 2,
            'description': f"Large divergence: {count} unique each + {count//2} shared"
        }

    def scenario_bucket_test(self) -> dict:
        """Events spread across multiple days for bucket testing."""
        alice_events = self.generate_across_buckets(
            prefix="alice",
            events_per_bucket=3,
            bucket_level="day",
            num_buckets=5
        )
        bob_events = self.generate_across_buckets(
            prefix="bob",
            events_per_bucket=3,
            bucket_level="day",
            num_buckets=5
        )
        return {
            'alice': alice_events,
            'bob': bob_events,
            'expected_total': 30,
            'description': "Bucket test: 3 events/day for 5 days each"
        }
