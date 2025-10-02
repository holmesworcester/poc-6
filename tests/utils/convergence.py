"""Convergence testing utility for event projection order independence."""
from typing import Any
import json
import base64
import random
import itertools


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
    event_ids = _get_all_first_seen_ids(db)

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
        _project_in_order(ordering, db)

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


def _get_all_first_seen_ids(db: Any) -> list[str]:
    """Find all first_seen events by parsing blobs."""
    rows = db.query("SELECT id, blob FROM store")
    first_seen_ids = []

    for row in rows:
        try:
            blob = row['blob']
            # Try to parse as JSON
            event_data = json.loads(blob.decode('utf-8'))
            if event_data.get('type') == 'first_seen':
                # Encode id as base64 (matching store.py format)
                event_id = base64.b64encode(row['id']).decode('ascii')
                first_seen_ids.append(event_id)
        except:
            continue  # Skip encrypted/non-JSON blobs

    return first_seen_ids


def _get_projection_tables(db: Any) -> list[str]:
    """Query sqlite_master for all tables except store and incoming_blobs."""
    rows = db.query("""
        SELECT name FROM sqlite_master
        WHERE type='table'
        AND name NOT IN ('store', 'incoming_blobs', 'sqlite_sequence')
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


def _project_in_order(event_ids: list[str], db: Any) -> None:
    """Project first_seen events in the given order."""
    from events import first_seen

    for event_id in event_ids:
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
