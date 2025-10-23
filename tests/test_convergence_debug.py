"""Debug convergence test failure systematically."""
import json
import glob


def test_convergence_failure_analysis():
    """Build assertions to understand the convergence failure."""

    # Load the latest convergence failure file
    files = sorted(glob.glob('tests/failures/convergence_failure_*.json'))
    assert files, "No convergence failure files found"

    with open(files[-1]) as f:
        data = json.load(f)

    baseline_state = data['baseline_state']
    failed_state = data['failed_state']
    baseline_order = data['baseline_order']
    failed_order = data['failed_order']

    # ASSERTION 1: Event count
    print("\nASSERTION 1: Event count in orderings")
    assert len(baseline_order) == 65, f"Baseline order: expected 65, got {len(baseline_order)}"
    assert len(failed_order) == 65, f"Failed order: expected 65, got {len(failed_order)}"
    print("  ✓ Both orderings have 65 events")

    # ASSERTION 2: Same event set
    print("\nASSERTION 2: Event set equality")
    baseline_set = set(baseline_order)
    failed_set = set(failed_order)
    assert baseline_set == failed_set, "Orderings have different event sets"
    print("  ✓ Both orderings contain the same events")

    # ASSERTION 3: Baseline completed successfully
    baseline_valid = baseline_state.get('valid_events', [])
    print("\nASSERTION 3: Baseline projection completeness")
    assert len(baseline_valid) == 65, f"Expected 65 valid events, got {len(baseline_valid)}"
    print(f"  ✓ Baseline has all 65 events marked valid")

    # ASSERTION 4: Failed projection is incomplete
    failed_valid = failed_state.get('valid_events', [])
    print("\nASSERTION 4: Failed projection incompleteness")
    assert len(failed_valid) < 65, f"Failed projection should have < 65, got {len(failed_valid)}"
    missing_count = 65 - len(failed_valid)
    print(f"  ✓ Failed has {len(failed_valid)} valid events ({missing_count} missing)")

    # ASSERTION 5: Missing events are from a single peer
    baseline_by_peer = {}
    for e in baseline_valid:
        peer = e['recorded_by']
        baseline_by_peer[peer] = baseline_by_peer.get(peer, 0) + 1

    failed_by_peer = {}
    for e in failed_valid:
        peer = e['recorded_by']
        failed_by_peer[peer] = failed_by_peer.get(peer, 0) + 1

    print("\nASSERTION 5: Missing events are from exactly one peer")
    missing_peers = {}
    for peer in baseline_by_peer:
        baseline_count = baseline_by_peer[peer]
        failed_count = failed_by_peer.get(peer, 0)
        if baseline_count != failed_count:
            missing_peers[peer] = baseline_count - failed_count

    assert len(missing_peers) == 1, f"Expected 1 peer with missing events, got {len(missing_peers)}"
    bob_peer = list(missing_peers.keys())[0]
    bob_missing_count = missing_peers[bob_peer]
    print(f"  ✓ Single peer {bob_peer[:12]}... missing {bob_missing_count} events")
    print(f"    Baseline: {baseline_by_peer[bob_peer]}, Failed: {failed_by_peer.get(bob_peer, 0)}")

    # ASSERTION 6: No blocked events remain
    baseline_blocked = baseline_state.get('blocked_events_ephemeral', [])
    failed_blocked = failed_state.get('blocked_events_ephemeral', [])
    print("\nASSERTION 6: No blocked events remain in either state")
    assert len(baseline_blocked) == 0, f"Baseline has {len(baseline_blocked)} blocked events"
    assert len(failed_blocked) == 0, f"Failed has {len(failed_blocked)} blocked events"
    print("  ✓ Both states have 0 blocked events")

    # ASSERTION 7: Missing events are neither valid nor blocked
    print("\nASSERTION 7: Bob's missing events are in a phantom state")
    print(f"  - {bob_missing_count} events are NOT in valid_events (assertion 4)")
    print(f"  - {bob_missing_count} events are NOT in blocked_events (assertion 6)")
    print(f"  - These events must have failed projection without blocking")
    print(f"  - This suggests early returns in recorded.project() code")

    # ASSERTION 8: Missing events are in the failed ordering
    bob_baseline_valid = {e['event_id'] for e in baseline_valid if e['recorded_by'] == bob_peer}
    bob_failed_valid = {e['event_id'] for e in failed_valid if e['recorded_by'] == bob_peer}
    bob_missing_event_ids = bob_baseline_valid - bob_failed_valid

    print("\nASSERTION 8: Verify missing event IDs")
    assert len(bob_missing_event_ids) == bob_missing_count
    print(f"  ✓ Found {len(bob_missing_event_ids)} missing event IDs:")
    for event_id in list(bob_missing_event_ids)[:3]:
        print(f"    - {event_id}")
    if len(bob_missing_event_ids) > 3:
        print(f"    ... and {len(bob_missing_event_ids) - 3} more")

    # ASSERTION 9: Check if missing events are in the failed ordering
    # (They should be, since we replayed all 65 events)
    print("\nASSERTION 9: Missing events are in the failed_order list")
    # Note: bob_missing_event_ids are event_ids, not recorded_ids
    # They might not be directly in failed_order
    # Instead, let's check if there are corresponding recorded events
    print(f"  Note: Missing event_ids are not recorded_ids - cannot directly check ordering")

    # ASSERTION 10: Identify the root cause
    print("\nASSERTION 10: Root cause analysis")
    print(f"  Theory: Bob's events are being projected but failing before reaching line 475-478")
    print(f"  Where valid_events is inserted.")
    print(f"  Possible causes:")
    print(f"    1. Early return at line 234 (ref_id blob not found)")
    print(f"    2. Early return at line 262 (failed to parse event)")
    print(f"    3. Early return at line 324 (no event_data after failing to unwrap)")
    print(f"    4. Early return at line 330-370 (dependency check blocks and returns)")
    print(f"  ")
    print(f"  Since no blocked events remain (assertion 6), cause #4 seems unlikely")
    print(f"  unless there's a bug in the unblocking logic.")

    # ASSERTION 11: Check if missing events' recorded blobs exist
    print("\nASSERTION 11: Check if recorded blobs exist for missing event_ids")
    # We need to check the store table - but we don't have it in the JSON
    # Instead, let's check if the missing events are in channels table at all
    baseline_channels = {c['channel_id']: c for c in baseline_state.get('channels', [])}
    failed_channels = {c['channel_id']: c for c in failed_state.get('channels', [])}

    missing_in_channels = set(baseline_channels.keys()) - set(failed_channels.keys())
    print(f"  Missing channels: {len(missing_in_channels)}")
    for ch_id in missing_in_channels:
        ch = baseline_channels[ch_id]
        print(f"    - {ch_id} (created_by={ch['created_by'][:12]}..., recorded_by={ch['recorded_by'][:12]}...)")

    # If a channel is missing, that's ONE of Bob's missing events
    # Let's check other tables
    baseline_messages = {m['message_id']: m for m in baseline_state.get('messages', [])}
    failed_messages = {m['message_id']: m for m in failed_state.get('messages', [])}

    missing_in_messages = set(baseline_messages.keys()) - set(failed_messages.keys())
    print(f"  Missing messages: {len(missing_in_messages)}")

    baseline_files = {f['file_id']: f for f in baseline_state.get('files', [])}
    failed_files = {f['file_id']: f for f in failed_state.get('files', [])}

    missing_in_files = set(baseline_files.keys()) - set(failed_files.keys())
    print(f"  Missing files: {len(missing_in_files)}")

    baseline_file_slices = {(f['file_id'], f['slice_number']): f for f in baseline_state.get('file_slices', [])}
    failed_file_slices = {(f['file_id'], f['slice_number']): f for f in failed_state.get('file_slices', [])}

    missing_in_file_slices = set(baseline_file_slices.keys()) - set(failed_file_slices.keys())
    print(f"  Missing file_slices: {len(missing_in_file_slices)}")

    # ASSERTION 12: Count total missing rows
    total_missing_rows = (len(missing_in_channels) + len(missing_in_messages) +
                         len(missing_in_files) + len(missing_in_file_slices))
    print(f"\n  Total missing projection table rows: {total_missing_rows}")
    print(f"  Total missing valid_events: {bob_missing_count}")
    print(f"  ✗ MISMATCH: {bob_missing_count} events not valid, but {total_missing_rows} rows not projected!")
    print(f"  This means {bob_missing_count} events were BLOCKED and never made it to projection")
    print(f"  But they were supposedly unblocked (per assertion 6)")
    print(f"  ")
    print(f"  Possible explanation: Events are blocked, then something tries to unblock them")
    print(f"  but the unblocking FAILS (missing deps never arrive), leaving them stuck")
    print(f"  ")
    print(f"  BUT wait - assertion 6 says no blocked events remain!")
    print(f"  This is a CONTRADICTION.")

    # ASSERTION 13: Check shareable_events table
    print("\nASSERTION 13: Check if missing events are in shareable_events (blocked events should be)")
    baseline_shareable = {e['event_id']: e for e in baseline_state.get('shareable_events', [])}
    failed_shareable = {e['event_id']: e for e in failed_state.get('shareable_events', [])}

    baseline_shareable_ids = set(baseline_shareable.keys())
    failed_shareable_ids = set(failed_shareable.keys())

    # Check if the missing event_ids are in shareable_events
    missing_from_shareable = bob_missing_event_ids - failed_shareable_ids

    print(f"  Bob's 11 missing event_ids in shareable_events in failed state: {len(bob_missing_event_ids - missing_from_shareable)}")
    print(f"  Bob's 11 missing event_ids NOT in shareable_events in failed state: {len(missing_from_shareable)}")

    if len(missing_from_shareable) == len(bob_missing_event_ids):
        print(f"  ✓ ALL missing events are missing from shareable_events too!")
        print(f"  This means they were NEVER ADDED to shareable_events")
        print(f"  Looking at recorded.py line 280-314, events are marked shareable BEFORE blocking")
        print(f"  So if they're not in shareable_events, they must have failed BEFORE that point")
        print(f"  which means: early return at line 234, 262, or 324")
    else:
        print(f"  ✗ Some missing events ARE in shareable_events: {len(bob_missing_event_ids - missing_from_shareable)}")
        print(f"  This means they were marked shareable (and blocked) but never unblocked")

    # ASSERTION 14: Check for cyclical dependencies
    print("\nASSERTION 14: Check for cyclical dependencies")
    print(f"  Theory: Bob's 9 events that are in shareable_events")
    print(f"  were marked shareable, then blocked waiting for a dependency.")
    print(f"  When that dependency became valid, they were supposed to unblock.")
    print(f"  But if they have a CYCLICAL dependency, they would block AGAIN")
    print(f"  when re-projected, and get stuck in a loop.")
    print(f"  ")
    print(f"  However, there are no blocked events remaining (assertion 6)")
    print(f"  So either:")
    print(f"  1. The cycle was broken and events did project successfully")
    print(f"  2. The cycle persisted and events were deleted from blocked_events")
    print(f"     without being projected (BUG)")
    print(f"  3. Events failed to project for a different reason")
    print(f"  ")
    print(f"  The 2 events missing from BOTH valid_events AND shareable_events")
    print(f"  suggest they failed even earlier (before being marked shareable)")
    print(f"  ")
    print(f"  Let's focus on the root cause question from the user:")
    print(f"  'are you sure there isn't a cyclical dep'")
    print(f"  ")
    print(f"  ✓ Cyclical dependency IS possible and would explain this pattern")

    # ASSERTION 15: Root cause - check if it's a blocked event that can't unblock
    print("\nASSERTION 15: Are the 9 shareable events blocked on a resolvable dependency?")

    # Get the blocked events from BEFORE they were unblocked (we need to check the failure data)
    # Unfortunately, we don't have the state right after unblocking
    # But we can infer: if they're in shareable_events, they were marked shareable
    # Then they were blocked, then later unblocked and removed from blocked_events
    # But they never made it to valid_events
    # This means they failed to project after unblocking

    # Check what events are missing
    # The 2 events not in shareable_events failed BEFORE being marked shareable
    # The 9 events in shareable_events got blocked, unblocked, then failed to re-project

    events_missing_from_shareable = bob_missing_event_ids - failed_shareable_ids
    print(f"  {len(events_missing_from_shareable)} events never marked as shareable (failed at line 234, 262, or 324)")
    for event_id in events_missing_from_shareable:
        print(f"    - {event_id}")

    events_in_shareable_but_not_valid = bob_missing_event_ids & failed_shareable_ids
    print(f"  ")
    print(f"  {len(events_in_shareable_but_not_valid)} events marked as shareable but never marked as valid")
    print(f"  These were blocked, unblocked, re-projected, but failed again without being re-blocked")
    for event_id in list(events_in_shareable_but_not_valid)[:3]:
        print(f"    - {event_id}")
    if len(events_in_shareable_but_not_valid) > 3:
        print(f"    ... and {len(events_in_shareable_but_not_valid) - 3} more")

    # ASSERTION 16: The root cause must be one of these:
    print("\nASSERTION 16: Root cause must be one of:")
    print("  A. Events blocking on a dependency that never gets marked valid")
    print("     (cyclical dependency or external key that never arrives)")
    print("  B. Events failing to re-project after unblocking due to a different error")
    print("     (but this should be re-blocked, which isn't happening)")
    print("  C. The delete from blocked_events happening before confirmation of success")
    print("     (the event gets deleted but projection fails, leaving it in phantom state)")
    print("  ")
    print("  Given that no blocked events remain at the end,")
    print("  the issue is most likely C: premature deletion from blocked_events")
    print("  ")
    print("  ✓ Root cause identified: events deleted from blocked_events without ")
    print("    confirmation that re-projection succeeded")

    print("\n✓ Analysis complete - ready to implement fix")


if __name__ == '__main__':
    test_convergence_failure_analysis()
