"""Convergence and idempotency testing utilities for event projection."""
from typing import Any
import json
import random
import itertools
import crypto


def assert_reprojection(db: Any) -> None:
    """Test that projection state can be restored from event store.

    This is the purest test: proves that the event store is the complete
    source of truth and projection tables can be rebuilt from scratch.

    Args:
        db: Database connection (in-memory for tests)

    Raises:
        AssertionError: If restored state differs from original
    """
    # Get all first_seen event IDs in original order
    event_ids = _get_projectable_event_ids(db)

    if not event_ids:
        print("⚠ No first_seen events found, skipping re-projection test")
        return

    print(f"Testing re-projection of {len(event_ids)} events from blank slate")

    # Capture current state
    baseline_state = _dump_projection_state(db)

    # Drop and recreate all projection tables (blank slate)
    _recreate_projection_tables(db)

    # Re-project all events in original order
    _replay_events(event_ids, db)

    # Compare states
    current_state = _dump_projection_state(db)
    equal, diff_msg = _states_equal(baseline_state, current_state)

    if not equal:
        print(f"\n❌ Re-projection FAILED")
        print(f"Difference: {diff_msg}")
        raise AssertionError(f"Cannot restore state from event store! {diff_msg}")

    print(f"✓ Re-projection test passed: restored {len(event_ids)} events from blank slate")


def assert_idempotency(db: Any, num_trials: int = 10, max_repetitions: int = 5) -> None:
    """Test that projecting events multiple times produces identical final state.

    Args:
        db: Database connection (in-memory for tests)
        num_trials: Number of different repetition patterns to test (default 10)
        max_repetitions: Maximum times to project each event (default 5)

    Raises:
        AssertionError: If any repetition pattern produces different final state
    """
    # Get all first_seen event IDs in original order
    event_ids = _get_projectable_event_ids(db)

    if not event_ids:
        print("⚠ No first_seen events found, skipping idempotency test")
        return

    print(f"Testing {num_trials} repetition patterns of {len(event_ids)} events")

    # Capture baseline state (events already projected once)
    baseline_state = _dump_projection_state(db)

    # Test different repetition patterns
    for trial in range(num_trials):
        # Reset projection tables
        _recreate_projection_tables(db)

        # Generate random repetition counts for each event (1 to max_repetitions)
        repetitions = [random.randint(1, max_repetitions) for _ in event_ids]

        # Project each event the specified number of times
        _project_with_repetitions(event_ids, repetitions, db)

        # Compare state
        current_state = _dump_projection_state(db)
        equal, diff_msg = _states_equal(baseline_state, current_state)

        if not equal:
            print(f"\n❌ Idempotency FAILED on trial #{trial + 1}")
            print(f"Event order: {event_ids}")
            print(f"Repetitions: {repetitions}")
            print(f"Difference: {diff_msg}")
            raise AssertionError(f"Projection is not idempotent! {diff_msg}")

    print(f"✓ Idempotency test passed: {num_trials} repetition patterns produced identical state")


def assert_convergence(db: Any, num_trials: int = 10) -> None:
    """Test that projecting events in different orders produces identical final state.

    Args:
        db: Database connection (in-memory for tests)
        num_trials: Number of random orderings to test (default 10)
                   If total events <= 8, tests ALL permutations instead

    Raises:
        AssertionError: If any ordering produces different final state
    """
    # Get all first_seen event IDs
    event_ids = _get_projectable_event_ids(db)

    if not event_ids:
        print("⚠ No first_seen events found, skipping convergence test")
        return

    # Generate orderings
    if len(event_ids) <= 8:
        # Small dataset: test ALL permutations
        orderings = list(itertools.permutations(event_ids))
        print(f"Testing {len(orderings)} permutations of {len(event_ids)} events")
    else:
        # Large dataset: random sample
        orderings = [event_ids]  # Original order
        for _ in range(num_trials - 1):
            shuffled = event_ids.copy()
            random.shuffle(shuffled)
            orderings.append(shuffled)
        print(f"Testing {num_trials} random orderings of {len(event_ids)} events")

    # Project in original order and capture baseline
    baseline_state = _dump_projection_state(db)

    # Test each alternative ordering
    for i, ordering in enumerate(orderings[1:], start=1):
        # Reset projection tables
        _recreate_projection_tables(db)

        # Project in this order
        _replay_events(ordering, db)

        # Compare state
        current_state = _dump_projection_state(db)
        equal, diff_msg = _states_equal(baseline_state, current_state)

        if not equal:
            print(f"\n❌ Convergence FAILED on ordering #{i}")
            print(f"Baseline order: {event_ids}")
            print(f"Failed order:   {ordering}")
            print(f"Difference: {diff_msg}")
            raise AssertionError(f"Projection order matters! {diff_msg}")

    print(f"✓ Convergence test passed: {len(orderings)} orderings produced identical state")


def _get_projectable_event_ids(db: Any) -> list[str]:
    """Find all first_seen events for re-projection."""
    rows = db.query("SELECT id, blob FROM store ORDER BY rowid")
    event_ids = []

    for row in rows:
        try:
            blob = row['blob']
            # Try to parse as JSON
            event_data = crypto.parse_json(blob)
            event_type = event_data.get('type')

            # Only first_seen events (all events now have first_seen wrappers)
            if event_type == 'first_seen':
                # Encode id as base64 (matching store.py format)
                event_id = crypto.b64encode(row['id'])
                event_ids.append(event_id)
        except:
            continue  # Skip encrypted/non-JSON blobs

    return event_ids


def _get_projection_tables(db: Any) -> list[str]:
    """Query sqlite_master for all tables except store, incoming_blobs, and test fixtures."""
    rows = db.query("""
        SELECT name FROM sqlite_master
        WHERE type='table'
        AND name NOT IN ('store', 'incoming_blobs', 'sqlite_sequence', 'pre_keys')
        ORDER BY name
    """)
    return [row['name'] for row in rows]


def _clear_projection_tables(db: Any) -> None:
    """Delete all rows from projection tables, keep store intact."""
    tables = _get_projection_tables(db)

    for table in tables:
        db.execute(f"DELETE FROM {table}")

    db.commit()


def _recreate_projection_tables(db: Any) -> None:
    """Drop and recreate all projection tables using schema.create_all()."""
    import schema

    # Drop all projection tables
    tables = _get_projection_tables(db)
    for table in tables:
        db.execute(f"DROP TABLE IF EXISTS {table}")

    # Recreate using schema.create_all()
    # This will skip creating 'store' (already exists)
    schema.create_all(db)


def _replay_events(event_ids: list[str], db: Any) -> None:
    """Replay first_seen events in order, then process blocked queue."""
    from events import first_seen
    import queues

    # Project all events
    for event_id in event_ids:
        first_seen.project(event_id, db)

    # Process blocked events until queue is empty
    max_iterations = 100
    for iteration in range(max_iterations):
        peer_rows = db.query("SELECT DISTINCT seen_by_peer_id FROM blocked_events")
        if not peer_rows:
            break

        had_progress = False
        for peer_row in peer_rows:
            unblocked_first_seen_ids = queues.blocked.process(peer_row['seen_by_peer_id'], db)
            if unblocked_first_seen_ids:
                had_progress = True
                # Re-project unblocked first_seen events
                for first_seen_id in unblocked_first_seen_ids:
                    first_seen.project(first_seen_id, db)

        if not had_progress:
            break

    db.commit()


def _project_with_repetitions(event_ids: list[str], repetitions: list[int], db: Any) -> None:
    """Project first_seen events with repetitions."""
    from events import first_seen

    for event_id, count in zip(event_ids, repetitions):
        for _ in range(count):
            first_seen.project(event_id, db)

    db.commit()


def _dump_projection_state(db: Any) -> dict[str, list[dict]]:
    """
    Capture all projection table data.
    Returns: {table_name: [rows...], ...}
    Each row list is sorted by primary key for deterministic comparison.
    """
    tables = _get_projection_tables(db)
    state = {}

    for table in tables:
        rows = db.query(f"SELECT * FROM {table}")
        # Convert rows to dicts and sort by all columns as tuple for deterministic ordering
        row_dicts = [dict(row) for row in rows]
        # Sort by all columns as tuple for deterministic ordering
        row_dicts.sort(key=lambda r: tuple(str(v) for v in r.values()))
        state[table] = row_dicts

    return state


def _states_equal(state1: dict, state2: dict) -> tuple[bool, str]:
    """Deep compare two states. Returns (equal, diff_message)."""

    # Check same tables
    if set(state1.keys()) != set(state2.keys()):
        tables_only_in_1 = set(state1.keys()) - set(state2.keys())
        tables_only_in_2 = set(state2.keys()) - set(state1.keys())
        msg = f"Different tables. Only in state1: {tables_only_in_1}, Only in state2: {tables_only_in_2}"
        return False, msg

    # Check each table
    for table in state1.keys():
        rows1 = state1[table]
        rows2 = state2[table]

        if len(rows1) != len(rows2):
            msg = f"Table '{table}': {len(rows1)} rows vs {len(rows2)} rows"
            return False, msg

        if rows1 != rows2:
            msg = f"Table '{table}': row contents differ\nState1: {rows1}\nState2: {rows2}"
            return False, msg

    return True, ""
