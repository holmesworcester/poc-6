#!/usr/bin/env python3
"""Compare baseline vs failed event processing order."""
import json

with open('tests/failures/convergence_failure_1761247864_ordering_1.json') as f:
    data = json.load(f)

baseline_order = data['baseline_order']
failed_order = data['failed_order']

print("Looking for TWO SPECIFIC channel recordings:\n")
print("Expected from baseline_state:")
for ch in data['baseline_state']['channels']:
    print(f"  channel_id={ch['channel_id'][:20]}... recorded_by={ch['recorded_by'][:20]}...\n")

# Create a mapping of recorded_id to ref_type
store_map = {}
for row in data['store']:
    try:
        if isinstance(row['blob'], str):
            # It's JSON-like
            if row['blob'].startswith('{'):
                import json as j
                blob_data = j.loads(row['blob'])
                if blob_data.get('type') == 'recorded':
                    ref_id = blob_data.get('ref_id')
                    recorded_by = blob_data.get('recorded_by')
                    store_map[row['id']] = (ref_id, recorded_by, 'recorded')
    except:
        pass

print("\n" + "="*70)
print("BASELINE ORDER - Position of channel events:")
print("="*70)
for i, recorded_id in enumerate(baseline_order):
    if recorded_id in store_map:
        ref_id, recorded_by, typ = store_map[recorded_id]
        if ref_id == 'Owv1/axNY/ITKlitDR6w':  # The channel ID we're looking for
            print(f"[{i:2d}] {recorded_id[:15]}... -> ref={ref_id[:15]}... by {recorded_by[:15]}...")

print("\n" + "="*70)
print("FAILED ORDER - Position of same channel events:")
print("="*70)
for i, recorded_id in enumerate(failed_order):
    if recorded_id in store_map:
        ref_id, recorded_by, typ = store_map[recorded_id]
        if ref_id == 'Owv1/axNY/ITKlitDR6w':  # The channel ID we're looking for
            print(f"[{i:2d}] {recorded_id[:15]}... -> ref={ref_id[:15]}... by {recorded_by[:15]}...")

print("\n" + "="*70)
print("ANALYSIS:")
print("="*70)
print("The question is: why do channels project successfully in baseline but not failed?")
print("Is it because:")
print("  1. They're blocked on a dependency in failed order that's not in baseline?")
print("  2. They're processed in different order relative to their dependencies?")
print("  3. Some dependency is missing or becomes unavailable in failed order?")
