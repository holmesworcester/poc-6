"""Convergence and idempotency testing utilities for event projection."""
from typing import Any
import json
import random
import itertools
import time
import os
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
    # Get all recorded event IDs in original order
    event_ids = _get_projectable_event_ids(db)

    if not event_ids:
        print("⚠ No recorded events found, skipping re-projection test")
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

        # Debug: Show channel details
        if 'channels' in baseline_state and 'channels' in current_state:
            print(f"\nBaseline channels ({len(baseline_state['channels'])} rows):")
            for ch in baseline_state['channels']:
                print(f"  {ch}")
            print(f"\nCurrent channels ({len(current_state['channels'])} rows):")
            for ch in current_state['channels']:
                print(f"  {ch}")

            # Show blocked events
            blocked = db.query("SELECT * FROM blocked_events_ephemeral")
            print(f"\nBlocked events after reprojection: {len(blocked)}")
            print(f"NOTE: Events blocked during reprojection indicate that some projection")
            print(f"depends on the sync protocol running, not just on replaying recorded events.")
            print(f"This is a known architectural issue where sync protocol state affects projection.")
            if blocked:
                print(f"\nALL {len(blocked)} blocked events:")
                import crypto as crypto_mod
                for i, b in enumerate(blocked):
                    # Try to get the event type
                    event_type = "unknown"
                    ref_id_short = "?"
                    try:
                        recorded_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (b['recorded_id'],))
                        if recorded_blob:
                            recorded_data = crypto_mod.parse_json(recorded_blob['blob'])
                            ref_id = recorded_data.get('ref_id', 'N/A')
                            ref_id_short = ref_id[:20] if ref_id != 'N/A' else '?'
                            ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                            if ref_blob:
                                try:
                                    ref_data = crypto_mod.parse_json(ref_blob['blob'])
                                    event_type = ref_data.get('type', 'unknown')
                                except:
                                    event_type = "encrypted"
                    except:
                        pass

                    print(f"  [{i+1}] type={event_type} ref_id={ref_id_short}... recorded_by={b['recorded_by'][:20]}... deps_remaining={b['deps_remaining']}")
                    print(f"      missing_deps: {b['missing_deps']}")

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
    # Get all recorded event IDs in original order
    event_ids = _get_projectable_event_ids(db)

    if not event_ids:
        print("⚠ No recorded events found, skipping idempotency test")
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


def assert_convergence(
    db: Any,
    num_trials: int = 1000,
    save_failures: bool = True,
    stop_on_first_failure: bool = True
) -> None:
    """Test that projecting events in different orders produces identical final state.

    Args:
        db: Database connection (in-memory for tests)
        num_trials: Number of random orderings to test (default 1000)
                   If total events <= 8, tests ALL permutations instead
        save_failures: Whether to save failed orderings to disk (default True)
        stop_on_first_failure: Whether to stop on first failure or continue testing (default True)

    Raises:
        AssertionError: If any ordering produces different final state
    """
    # Get all recorded event IDs
    event_ids = _get_projectable_event_ids(db)

    if not event_ids:
        print("⚠ No recorded events found, skipping convergence test")
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
    failures = []
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
            print(f"Failed order:   {list(ordering)}")
            print(f"Difference: {diff_msg}")

            # Save failure data with store table for replay
            store_rows = db.query("SELECT * FROM store")
            store_data = []
            for row in store_rows:
                store_data.append({
                    'id': row['id'],
                    'blob': row['blob'].decode('latin1') if isinstance(row['blob'], bytes) else row['blob'],
                    'stored_at': row['stored_at']
                })

            failure_data = {
                'ordering_number': i,
                'total_orderings': len(orderings),
                'baseline_order': event_ids,
                'failed_order': list(ordering),
                'baseline_state': baseline_state,
                'failed_state': current_state,
                'store': store_data,  # Add store table for replay
                'difference': diff_msg,
                'timestamp': time.time(),
                'test_name': 'convergence',
            }
            failures.append(failure_data)

            if save_failures:
                failure_file = _save_failure(failure_data)
                print(f"💾 Failure saved to: {failure_file}")

            if stop_on_first_failure:
                raise AssertionError(f"Projection order matters! {diff_msg}")

    if failures and not stop_on_first_failure:
        print(f"\n❌ Convergence test found {len(failures)} failures")
        raise AssertionError(f"Projection order matters! Found {len(failures)} different states")

    print(f"✓ Convergence test passed: {len(orderings)} orderings produced identical state")


def _get_projectable_event_ids(db: Any) -> list[str]:
    """Find all recorded events for re-projection."""
    rows = db.query("SELECT id, blob FROM store ORDER BY rowid")
    event_ids = []
    event_type_counts = {}

    for row in rows:
        try:
            blob = row['blob']
            # Try to parse as JSON
            event_data = crypto.parse_json(blob)
            event_type = event_data.get('type')

            # Only recorded events (all events now have recorded wrappers)
            if event_type == 'recorded':
                # ID is already TEXT (base64) in store
                event_id = row['id']
                event_ids.append(event_id)

                # Track what types are being replayed for debugging
                ref_id = event_data.get('ref_id')
                if ref_id:
                    ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                    if ref_blob:
                        try:
                            ref_data = crypto.parse_json(ref_blob['blob'])
                            ref_type = ref_data.get('type', 'unknown')
                            event_type_counts[ref_type] = event_type_counts.get(ref_type, 0) + 1
                        except:
                            event_type_counts['encrypted'] = event_type_counts.get('encrypted', 0) + 1
        except:
            continue  # Skip encrypted/non-JSON blobs

    # Debug output
    if event_type_counts:
        print(f"\nEvent types to replay:")
        for etype, count in sorted(event_type_counts.items()):
            print(f"  {etype}: {count}")

    return event_ids


def _get_projection_tables(db: Any) -> list[str]:
    """Query sqlite_master for all tables except store, incoming_blobs, ephemeral state, and test fixtures."""
    rows = db.query("""
        SELECT name FROM sqlite_master
        WHERE type='table'
        AND name NOT IN ('store', 'incoming_blobs', 'sqlite_sequence', 'pre_keys', 'transit_keys')
        AND name NOT LIKE '%_ephemeral'
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
    import sqlite3

    # Clear incoming_blobs (sync protocol messages that reference ephemeral state)
    # These must be cleared because they contain wrapped blobs with transit_key hints
    # that reference ephemeral transit_keys created during sync protocol
    db.execute("DELETE FROM incoming_blobs")

    # Clear blocked_events_ephemeral (events waiting for ephemeral state won't resolve after reprojection)
    db.execute("DELETE FROM blocked_events_ephemeral")

    # Drop all projection tables (disable foreign keys to avoid constraint errors)
    # Multiple attempts may be needed due to dependency ordering
    tables = _get_projection_tables(db)

    # Try to drop all tables, retrying if foreign key constraints fail
    max_attempts = len(tables) + 1
    for _ in range(max_attempts):
        remaining = []
        for table in tables:
            try:
                db._conn.execute(f"DROP TABLE IF EXISTS {table}")
            except sqlite3.IntegrityError:
                # Foreign key constraint - try again in next pass
                remaining.append(table)

        if not remaining:
            break
        tables = remaining
    else:
        # Still have tables after max attempts
        raise RuntimeError(f"Could not drop tables due to circular foreign key constraints: {remaining}")

    # Recreate using schema.create_all()
    # This will skip creating 'store' (already exists)
    schema.create_all(db)


def _replay_events(event_ids: list[str], db: Any) -> None:
    """Replay recorded events in order. Event-driven unblocking happens automatically."""
    from events.transit import recorded
    import crypto

    # Project all events - unblocking happens automatically via notify_event_valid()
    for i, event_id in enumerate(event_ids):
        # Log event type for debugging
        try:
            blob = db.query_one("SELECT blob FROM store WHERE id = ?", (event_id,))
            if blob:
                data = crypto.parse_json(blob['blob'])
                event_type = data.get('type', 'unknown')
                ref_id = data.get('ref_id', 'N/A') if event_type == 'recorded' else event_id
                if event_type == 'recorded' and ref_id != 'N/A':
                    ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                    if ref_blob:
                        try:
                            ref_data = crypto.parse_json(ref_blob['blob'])
                            wrapped_type = ref_data.get('type', 'unknown')
                            # Log all invite-related events for debugging
                            if wrapped_type in ['invite', 'invite_accepted'] or i < 3 or i >= len(event_ids) - 3:
                                print(f"  [{i+1}/{len(event_ids)}] Projecting {event_id[:12]}... (recorded→{wrapped_type})")
                        except:
                            if i < 3 or i >= len(event_ids) - 3:
                                print(f"  [{i+1}/{len(event_ids)}] Projecting {event_id[:12]}... (recorded→encrypted)")
        except:
            pass
        recorded.project(event_id, db)

    # Safety check: verify no events remain blocked (would indicate a bug) TODO: fix this: sometimes it will be normal for events to remain blocked, but unblockable events should unblock naturally
    remaining = db.query("SELECT COUNT(*) as count FROM blocked_events_ephemeral")
    if remaining and remaining[0]['count'] > 0:
        print(f"⚠️  Warning: {remaining[0]['count']} events still blocked after replay")
        blocked = db.query("SELECT recorded_id, recorded_by, missing_deps FROM blocked_events_ephemeral")
        for b in blocked:
            # Get event type for debugging
            import crypto
            import json
            deps = json.loads(b['missing_deps'])
            try:
                blob = db.query_one("SELECT blob FROM store WHERE id = ?", (b['recorded_id'],))
                if blob:
                    try:
                        data = crypto.parse_json(blob['blob'])
                        event_type = data.get('type', 'unknown')
                        if event_type == 'recorded':
                            ref_id = data.get('ref_id', 'N/A')
                            ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                            if ref_blob:
                                try:
                                    ref_data = crypto.parse_json(ref_blob['blob'])
                                    wrapped_type = ref_data.get('type', 'unknown')
                                    print(f"   - {b['recorded_id']} (recorded→{wrapped_type}) waiting for {len(deps)} deps:")
                                except:
                                    print(f"   - {b['recorded_id']} (recorded→encrypted) waiting for {len(deps)} deps:")
                            else:
                                print(f"   - {b['recorded_id']} (recorded→missing_ref) waiting for {len(deps)} deps:")
                        else:
                            print(f"   - {b['recorded_id']} ({event_type}) waiting for {len(deps)} deps:")
                    except:
                        print(f"   - {b['recorded_id']} (encrypted/unparseable) waiting for {len(deps)} deps:")
                else:
                    print(f"   - {b['recorded_id']} (not in store!) waiting for {len(deps)} deps:")

                # Check each dependency
                for dep_id in deps:
                    dep_in_store = db.query_one("SELECT 1 FROM store WHERE id = ?", (dep_id,))
                    dep_is_valid = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (dep_id, b['recorded_by']))
                    status = "✓valid" if dep_is_valid else ("in_store" if dep_in_store else "MISSING")
                    print(f"      {dep_id}: {status}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"   - {b['recorded_id']} (exception: {e}) waiting for: {b['missing_deps']}")

    db.commit()


def _project_with_repetitions(event_ids: list[str], repetitions: list[int], db: Any) -> None:
    """Project recorded events with repetitions."""
    from events.transit import recorded

    for event_id, count in zip(event_ids, repetitions):
        for _ in range(count):
            recorded.project(event_id, db)

    db.commit()


def _dump_projection_state(db: Any) -> dict[str, list[dict]]:
    """
    Capture all projection table data.
    Returns: {table_name: [rows...], ...}
    Each row list is sorted by primary key for deterministic comparison.

    Note: Bytes values are converted to base64 strings for JSON serialization.
    """
    tables = _get_projection_tables(db)
    state = {}

    for table in tables:
        rows = db.query(f"SELECT * FROM {table}")
        # Convert rows to dicts, converting bytes to base64 for JSON serialization
        row_dicts = []
        for row in rows:
            row_dict = {}
            for key, value in dict(row).items():
                # Convert bytes to base64 for JSON serialization
                if isinstance(value, bytes):
                    row_dict[key] = crypto.b64encode(value)
                else:
                    row_dict[key] = value
            row_dicts.append(row_dict)

        # Filter out ephemeral events from valid_events table
        # Ephemeral events (like sync) are not stored in the event log, so they won't be
        # re-projected during reprojection. We exclude them from comparison.
        if table == 'valid_events':
            # Get all event IDs from the store to identify which events are persisted
            persisted_event_ids = set(row['id'] for row in db.query("SELECT id FROM store"))
            row_dicts = [r for r in row_dicts if r.get('event_id') in persisted_event_ids]

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


def _save_failure(failure_data: dict, max_total_mb: float = 50.0) -> str:
    """Save failure data to JSON file with size cap. Returns path to saved file.

    Args:
        failure_data: Failure information to save
        max_total_mb: Maximum total size of all failure files in MB (default 50MB)

    Returns:
        Path to saved file, or empty string if skipped due to size cap
    """
    # Create failures directory
    failures_dir = os.path.join('tests', 'failures')
    os.makedirs(failures_dir, exist_ok=True)

    # Check current total size of failure files
    total_size = 0
    if os.path.exists(failures_dir):
        for filename in os.listdir(failures_dir):
            filepath = os.path.join(failures_dir, filename)
            if os.path.isfile(filepath):
                total_size += os.path.getsize(filepath)

    total_size_mb = total_size / (1024 * 1024)

    # Check if we've exceeded the cap
    if total_size_mb >= max_total_mb:
        print(f"⚠️  Skipping failure save: already have {total_size_mb:.1f}MB of failures (cap: {max_total_mb}MB)")
        print(f"   Delete old failures from {failures_dir}/ if you want to save new ones")
        return ""

    # Estimate size of this failure
    failure_json = json.dumps(failure_data, indent=2)
    failure_size_mb = len(failure_json.encode('utf-8')) / (1024 * 1024)

    # Check if this single failure would exceed the cap
    if total_size_mb + failure_size_mb > max_total_mb:
        print(f"⚠️  Skipping failure save: would exceed cap ({total_size_mb:.1f}MB + {failure_size_mb:.1f}MB > {max_total_mb}MB)")
        return ""

    # Generate filename with timestamp
    timestamp = int(failure_data['timestamp'])
    ordering_num = failure_data.get('ordering_number', 0)
    filename = f"convergence_failure_{timestamp}_ordering_{ordering_num}.json"
    filepath = os.path.join(failures_dir, filename)

    # Save to file
    with open(filepath, 'w') as f:
        f.write(failure_json)

    saved_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    new_total_mb = total_size_mb + saved_size_mb

    print(f"   Saved {saved_size_mb:.2f}MB (total: {new_total_mb:.1f}MB / {max_total_mb}MB)")

    return filepath


def replay_ordering(db: Any, ordering: list[str], expected_state: dict = None) -> dict:
    """Replay a specific event ordering and return final state.

    Args:
        db: Database connection
        ordering: List of recorded event IDs in desired order
        expected_state: Optional expected state to compare against

    Returns:
        Final projection state dict

    Raises:
        AssertionError: If state doesn't match expected_state (when provided)
    """
    # Reset to blank slate
    _recreate_projection_tables(db)

    # Replay in specified order
    _replay_events(ordering, db)

    # Capture final state
    final_state = _dump_projection_state(db)

    # Compare if expected provided
    if expected_state:
        equal, diff_msg = _states_equal(expected_state, final_state)
        if not equal:
            raise AssertionError(f"Replay produced different state: {diff_msg}")

    return final_state


def replay_from_file(db: Any, failure_file: str) -> dict:
    """Replay a convergence failure from a saved JSON file.

    Args:
        db: Database connection
        failure_file: Path to saved failure JSON

    Returns:
        Final projection state dict

    Raises:
        AssertionError: If replay produces different state than baseline
    """
    with open(failure_file, 'r') as f:
        failure_data = json.load(f)

    print(f"📂 Replaying failure from {failure_file}")
    print(f"   Ordering #{failure_data.get('ordering_number')} of {failure_data.get('total_orderings')}")
    print(f"   Baseline order: {failure_data['baseline_order']}")
    print(f"   Failed order:   {failure_data['failed_order']}")
    print(f"   Original diff:  {failure_data['difference']}")

    # Replay the failed ordering
    final_state = replay_ordering(
        db,
        failure_data['failed_order'],
        expected_state=failure_data['baseline_state']
    )

    print(f"✅ Replay completed")
    return final_state
