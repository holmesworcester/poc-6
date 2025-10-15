#!/usr/bin/env python3
"""Comprehensive convergence failure analyzer.

Provides detailed analysis of why orderings produce different states:
- Side-by-side event projection comparison
- Per-peer state differences
- Dependency chain analysis
- Missing data identification

Usage:
    python tests/utils/analyze_convergence.py tests/failures/convergence_failure_*.json
    python tests/utils/analyze_convergence.py tests/failures/convergence_failure_*.json --verbose
"""

import sys
import json
import sqlite3
from pathlib import Path
from typing import Any
import crypto
from db import Database
import schema


def load_failure(failure_file: str) -> dict:
    """Load failure data from JSON file."""
    with open(failure_file, 'r') as f:
        return json.load(f)


def setup_db(failure_data: dict) -> Database:
    """Create database with store and local_peers from failure data."""
    conn = sqlite3.Connection(':memory:')
    db = Database(conn)
    schema.create_all(db)

    # Restore store
    for row in failure_data['store']:
        db.execute(
            "INSERT INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
            (row['id'], row['blob'].encode('latin1'), row['stored_at'])
        )

    # Restore local_peers
    for row in failure_data['baseline_state']['local_peers']:
        db.execute(
            "INSERT INTO local_peers (peer_id, public_key, private_key, created_at) VALUES (?, ?, ?, ?)",
            (crypto.b64decode(row['peer_id']), crypto.b64decode(row['public_key']),
             crypto.b64decode(row['private_key']), row['created_at'])
        )

    return db


def replay_with_trace(db: Any, ordering: list[str], verbose: bool = False) -> dict:
    """Replay ordering and capture detailed projection trace.

    Returns:
        {
            'events': [
                {
                    'event_id': str,
                    'index': int,
                    'event_type': str,
                    'ref_id': str,
                    'recorded_by': str,
                    'projection_result': 'success' | 'failed' | 'blocked' | 'skipped',
                    'reason': str  # why it failed/blocked
                }
            ],
            'final_state': dict  # table snapshots
        }
    """
    from tests.utils.convergence import _recreate_projection_tables, _dump_projection_state
    from events.transit import recorded

    # Reset tables
    _recreate_projection_tables(db)

    trace = []

    for i, event_id in enumerate(ordering):
        event_info = {
            'event_id': event_id,
            'index': i,
            'event_type': 'unknown',
            'ref_id': None,
            'recorded_by': None,
            'projection_result': 'unknown',
            'reason': ''
        }

        # Get event metadata
        try:
            blob = db.query_one("SELECT blob FROM store WHERE id = ?", (event_id,))
            if blob:
                data = crypto.parse_json(blob['blob'])
                event_info['event_type'] = data.get('type', 'unknown')
                event_info['recorded_by'] = data.get('recorded_by', 'unknown')

                if event_info['event_type'] == 'recorded':
                    ref_id = data.get('ref_id')
                    event_info['ref_id'] = ref_id

                    # Get wrapped event type
                    if ref_id:
                        ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                        if ref_blob:
                            try:
                                ref_data = crypto.parse_json(ref_blob['blob'])
                                event_info['wrapped_type'] = ref_data.get('type', 'encrypted')
                            except:
                                event_info['wrapped_type'] = 'encrypted'
        except:
            pass

        # Project and capture result
        try:
            result = recorded.project(event_id, db)
            if result and result[0] is not None:
                event_info['projection_result'] = 'success'
            elif result and result[0] is None:
                # Check if blocked
                recorded_id = result[1] if len(result) > 1 else None
                if recorded_id:
                    blocked = db.query_one(
                        "SELECT 1 FROM blocked_events_ephemeral WHERE recorded_id = ?",
                        (recorded_id,)
                    )
                    if blocked:
                        event_info['projection_result'] = 'blocked'
                        event_info['reason'] = 'missing dependencies'
                    else:
                        event_info['projection_result'] = 'skipped'
                        event_info['reason'] = 'not for this peer (could not decrypt)'

            if verbose and event_info['projection_result'] in ['blocked', 'skipped']:
                print(f"  [{i+1}] {event_info.get('wrapped_type', 'unknown')} "
                      f"({event_id[:12]}...) by {event_info['recorded_by'][:12] if event_info['recorded_by'] else 'N/A'}... "
                      f"→ {event_info['projection_result']}: {event_info['reason']}")
        except Exception as e:
            event_info['projection_result'] = 'error'
            event_info['reason'] = str(e)

        trace.append(event_info)

    return {
        'events': trace,
        'final_state': _dump_projection_state(db)
    }


def compare_traces(baseline_trace: dict, failed_trace: dict) -> dict:
    """Compare two traces to find differences in projection outcomes."""
    differences = []

    baseline_events = {e['event_id']: e for e in baseline_trace['events']}
    failed_events = {e['event_id']: e for e in failed_trace['events']}

    all_event_ids = set(baseline_events.keys()) | set(failed_events.keys())

    for event_id in all_event_ids:
        base_e = baseline_events.get(event_id)
        fail_e = failed_events.get(event_id)

        if not base_e or not fail_e:
            continue

        if base_e['projection_result'] != fail_e['projection_result']:
            differences.append({
                'event_id': event_id,
                'event_type': base_e.get('wrapped_type', base_e.get('event_type')),
                'recorded_by': base_e['recorded_by'],
                'baseline_result': base_e['projection_result'],
                'failed_result': fail_e['projection_result'],
                'baseline_reason': base_e.get('reason', ''),
                'failed_reason': fail_e.get('reason', ''),
                'baseline_index': base_e['index'],
                'failed_index': fail_e['index']
            })

    return differences


def analyze_table_diff(table_name: str, baseline_rows: list, failed_rows: list) -> dict:
    """Detailed analysis of table differences grouped by recorded_by."""
    analysis = {
        'table': table_name,
        'baseline_count': len(baseline_rows),
        'failed_count': len(failed_rows),
        'per_peer': {}
    }

    # Group by recorded_by if present
    if baseline_rows and 'recorded_by' in baseline_rows[0]:
        baseline_by_peer = {}
        for row in baseline_rows:
            peer = row['recorded_by']
            if peer not in baseline_by_peer:
                baseline_by_peer[peer] = []
            baseline_by_peer[peer].append(row)

        failed_by_peer = {}
        for row in failed_rows:
            peer = row['recorded_by']
            if peer not in failed_by_peer:
                failed_by_peer[peer] = []
            failed_by_peer[peer].append(row)

        all_peers = set(baseline_by_peer.keys()) | set(failed_by_peer.keys())

        for peer in all_peers:
            base_count = len(baseline_by_peer.get(peer, []))
            fail_count = len(failed_by_peer.get(peer, []))

            status = 'SAME' if base_count == fail_count else 'DIFFERENT'
            missing_in_failed = base_count > fail_count
            extra_in_failed = fail_count > base_count

            analysis['per_peer'][peer] = {
                'baseline_count': base_count,
                'failed_count': fail_count,
                'status': status,
                'missing_in_failed': missing_in_failed,
                'extra_in_failed': extra_in_failed,
                'baseline_rows': baseline_by_peer.get(peer, []),
                'failed_rows': failed_by_peer.get(peer, [])
            }
    else:
        analysis['ungrouped'] = {
            'baseline_rows': baseline_rows,
            'failed_rows': failed_rows
        }

    return analysis


def print_analysis(failure_data: dict, baseline_trace: dict, failed_trace: dict, verbose: bool = False):
    """Print comprehensive analysis of failure."""
    print("=" * 80)
    print("CONVERGENCE FAILURE ANALYSIS")
    print("=" * 80)

    print(f"\nFailure: Ordering #{failure_data['ordering_number']} of {failure_data['total_orderings']}")
    print(f"Total events: {len(failure_data['baseline_order'])}")
    print(f"Difference: {failure_data['difference']}")

    # Event projection comparison
    print("\n" + "=" * 80)
    print("EVENT PROJECTION COMPARISON")
    print("=" * 80)

    diff_events = compare_traces(baseline_trace, failed_trace)

    if diff_events:
        print(f"\nFound {len(diff_events)} events with different projection outcomes:\n")
        for diff in diff_events:
            print(f"Event: {diff['event_type']} ({diff['event_id'][:20]}...)")
            print(f"  Recorded by: {diff['recorded_by'][:20]}...")
            print(f"  Position: baseline[{diff['baseline_index']}] → failed[{diff['failed_index']}]")
            print(f"  Baseline: {diff['baseline_result']}", end="")
            if diff['baseline_reason']:
                print(f" ({diff['baseline_reason']})", end="")
            print()
            print(f"  Failed:   {diff['failed_result']}", end="")
            if diff['failed_reason']:
                print(f" ({diff['failed_reason']})", end="")
            print("\n")
    else:
        print("\n✓ All events projected identically (difference is in table contents only)\n")

    # Table comparison
    print("=" * 80)
    print("TABLE STATE COMPARISON")
    print("=" * 80)

    baseline_state = baseline_trace['final_state']
    failed_state = failed_trace['final_state']

    for table in sorted(baseline_state.keys()):
        baseline_rows = baseline_state[table]
        failed_rows = failed_state.get(table, [])

        if baseline_rows != failed_rows:
            print(f"\n📊 Table: {table}")
            print(f"   Baseline: {len(baseline_rows)} rows")
            print(f"   Failed:   {len(failed_rows)} rows")

            analysis = analyze_table_diff(table, baseline_rows, failed_rows)

            if 'per_peer' in analysis and analysis['per_peer']:
                print(f"\n   Per-peer breakdown:")
                for peer, peer_data in analysis['per_peer'].items():
                    status_symbol = "✓" if peer_data['status'] == 'SAME' else "❌"
                    print(f"   {status_symbol} {peer[:20]}...: {peer_data['baseline_count']} → {peer_data['failed_count']} rows")

                    if peer_data['missing_in_failed'] and verbose:
                        print(f"      Missing in failed:")
                        for row in peer_data['baseline_rows']:
                            if row not in peer_data['failed_rows']:
                                print(f"        {row}")

                    if peer_data['extra_in_failed'] and verbose:
                        print(f"      Extra in failed:")
                        for row in peer_data['failed_rows']:
                            if row not in peer_data['baseline_rows']:
                                print(f"        {row}")
            elif verbose:
                print(f"\n   Baseline rows: {baseline_rows}")
                print(f"   Failed rows: {failed_rows}")

    # Blocked events summary
    print("\n" + "=" * 80)
    print("BLOCKED EVENTS")
    print("=" * 80)

    baseline_blocked = sum(1 for e in baseline_trace['events'] if e['projection_result'] == 'blocked')
    failed_blocked = sum(1 for e in failed_trace['events'] if e['projection_result'] == 'blocked')

    print(f"\nBaseline: {baseline_blocked} events remained blocked")
    print(f"Failed:   {failed_blocked} events remained blocked")


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Analyze convergence failures')
    parser.add_argument('failure_file', help='Path to failure JSON file')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    args = parser.parse_args()

    # Load failure
    print(f"Loading failure from: {args.failure_file}\n")
    failure_data = load_failure(args.failure_file)

    # Setup database
    print("Setting up database...")
    db = setup_db(failure_data)
    print(f"Loaded {len(failure_data['store'])} events from store")
    print(f"Loaded {len(failure_data['baseline_state']['local_peers'])} local peers\n")

    # Replay both orderings with traces
    print("Replaying baseline ordering...")
    baseline_trace = replay_with_trace(db, failure_data['baseline_order'], args.verbose)

    print("Replaying failed ordering...")
    failed_trace = replay_with_trace(db, failure_data['failed_order'], args.verbose)

    # Print analysis
    print_analysis(failure_data, baseline_trace, failed_trace, args.verbose)

    print("\n" + "=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)


if __name__ == '__main__':
    main()
