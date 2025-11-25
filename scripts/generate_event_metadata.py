#!/usr/bin/env python3
"""Generate EVENT_TYPE/SHAREABLE metadata for all event modules.

Run from project root:
    python scripts/generate_event_metadata.py

This reads the current hardcoded LOCAL_ONLY_TYPES and EPHEMERAL_TYPES
from recorded.py and generates the metadata to add to each module.
"""
import os
import re
from pathlib import Path

# Current hardcoded values from recorded.py
LOCAL_ONLY_TYPES = {
    'peer', 'transit_key', 'group_key', 'transit_prekey',
    'group_prekey', 'recorded', 'network_joined', 'invite_accepted',
    'sync_connect', 'purge_expired'
}

EPHEMERAL_TYPES = {'sync_connect', 'sync_request', 'sync_response', 'purge_expired'}

# TABLE_MAP from recorded.py
TABLE_MAP = {
    'channel': ('channels', 'channel_id'),
    'group': ('groups', 'group_id'),
    'peer_shared': ('peers_shared', 'peer_shared_id'),
    'user': ('users', 'user_id'),
    'transit_prekey_shared': ('transit_prekeys_shared', 'transit_prekey_shared_id'),
    'group_prekey_shared': ('group_prekeys_shared', 'group_prekey_shared_id'),
    'group_key_shared': ('group_keys_shared', 'group_key_shared_id'),
    'invite': ('invites', 'invite_id'),
    'message': ('messages', 'message_id'),
    'message_deletion': ('message_deletions', 'deletion_id'),
    'address': ('addresses', 'address_id'),
    'group_member': ('group_members', 'user_id'),
}


def get_event_type_from_file(filepath: Path) -> str | None:
    """Extract event type from file content or filename."""
    content = filepath.read_text()

    # Look for type = 'xxx' in event creation
    match = re.search(r"'type':\s*'(\w+)'", content)
    if match:
        return match.group(1)

    # Fall back to filename
    return filepath.stem


def main():
    events_dir = Path('events')

    # Find all event modules
    modules = []
    for subdir in ['identity', 'content', 'group', 'network']:
        subdir_path = events_dir / subdir
        if not subdir_path.exists():
            continue
        for filepath in subdir_path.glob('*.py'):
            if filepath.name.startswith('_'):
                continue
            # Skip non-event modules
            if filepath.name in ['sync.py', 'recorded.py', 'registry.py']:
                continue
            modules.append(filepath)

    print("# Event Metadata Migration")
    print()
    print("Add the following metadata to each event module after the docstring and before imports.")
    print()

    for filepath in sorted(modules):
        event_type = get_event_type_from_file(filepath)
        if not event_type:
            continue

        shareable = event_type not in LOCAL_ONLY_TYPES
        ephemeral = event_type in EPHEMERAL_TYPES
        projection_table = TABLE_MAP.get(event_type)

        print(f"## {filepath}")
        print("```python")
        print(f"# Registry metadata")
        print(f"EVENT_TYPE = '{event_type}'")
        print(f"SHAREABLE = {shareable}  # {'Syncs to other peers' if shareable else 'Local-only'}")
        print(f"EPHEMERAL = {ephemeral}")
        if projection_table:
            print(f"PROJECTION_TABLE = {projection_table}")
        else:
            print(f"PROJECTION_TABLE = None")
        print("```")
        print()


if __name__ == '__main__':
    main()
