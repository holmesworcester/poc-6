"""Timeline debugging for convergence tests.

Provides a simple in-memory event log that captures projection flow
in chronological order, making it easy to trace causality chains and
identify divergence points between different event orderings.

Usage:
    from tests.utils import timeline

    # Enable at test start
    timeline.enable(phase='baseline')

    # Log events
    timeline.log('proj_start', ref_id=event_id, ref_type='channel',
                 recorded_by=peer_id, triggered_by='initial')

    # Change phase for different orderings
    timeline.set_phase('ordering_1')

    # Export on failure
    timeline.export('debug_timeline.txt')
"""
import json

# Global timeline state
_timeline = []
_enabled = False
_current_phase = "unknown"


def enable(phase="baseline"):
    """Enable timeline logging.

    Args:
        phase: Label for the current phase (e.g., 'baseline', 'ordering_1')
    """
    global _enabled, _current_phase, _timeline
    _enabled = True
    _current_phase = phase
    _timeline = []


def disable():
    """Disable timeline logging."""
    global _enabled
    _enabled = False


def set_phase(phase):
    """Change the current phase label.

    Args:
        phase: New phase label
    """
    global _current_phase
    _current_phase = phase


def log(event_type, ref_id=None, ref_type=None, recorded_by=None,
        status=None, message=None, **kwargs):
    """Log a timeline entry.

    Args:
        event_type: Type of event (e.g., 'proj_start', 'blocked', 'unblock')
        ref_id: Event ID being processed
        ref_type: Event type (e.g., 'channel', 'message', 'group_key_shared')
        recorded_by: Peer ID
        status: Status (e.g., 'success', 'blocked', 'failed')
        message: Human-readable message
        **kwargs: Additional metadata
    """
    if not _enabled:
        return

    entry = {
        'seq': len(_timeline),
        'phase': _current_phase,
        'type': event_type,
        'ref_id': ref_id[:20] if ref_id else None,
        'ref_type': ref_type,
        'peer': recorded_by[:8] if recorded_by else None,
        'status': status,
        'msg': message,
    }

    # Add any extra metadata
    for k, v in kwargs.items():
        entry[k] = v

    _timeline.append(entry)


def export(filepath):
    """Export timeline as readable text file.

    Args:
        filepath: Path to write timeline to

    Returns:
        filepath: Path that was written to
    """
    lines = ["# CONVERGENCE DEBUG TIMELINE\n"]

    current_phase = None
    for entry in _timeline:
        # Add phase header when phase changes
        if entry['phase'] != current_phase:
            current_phase = entry['phase']
            lines.append(f"\n## {current_phase.upper()}\n")

        # Build main line
        parts = [f"[{entry['seq']:04d}]"]

        if entry['ref_type'] and entry['ref_id']:
            parts.append(f"{entry['ref_type']}:{entry['ref_id']}")
        elif entry['ref_id']:
            parts.append(f"{entry['ref_id']}")

        if entry['peer']:
            parts.append(f"peer={entry['peer']}")

        parts.append(f"→ {entry['type']}")

        if entry['status']:
            parts.append(f"[{entry['status']}]")

        if entry['msg']:
            parts.append(f": {entry['msg']}")

        lines.append(" ".join(parts))

        # Add extra metadata on following lines
        for k, v in entry.items():
            if k not in ['seq', 'phase', 'type', 'ref_id', 'ref_type', 'peer', 'status', 'msg']:
                if v is not None:
                    # Format lists/dicts nicely
                    if isinstance(v, (list, dict)):
                        v_str = json.dumps(v)
                        if len(v_str) > 60:
                            v_str = v_str[:60] + "..."
                        lines.append(f"      {k}={v_str}")
                    else:
                        lines.append(f"      {k}={v}")

    with open(filepath, 'w') as f:
        f.write('\n'.join(lines))

    return filepath


def clear():
    """Clear the timeline."""
    global _timeline
    _timeline = []


def get_entries():
    """Get all timeline entries.

    Returns:
        List of timeline entry dicts
    """
    return _timeline.copy()
