# Changes to recorded.py

This shows how `recorded.py` would be simplified using the registry.

## Before (Current Code)

```python
# Line ~352 - hardcoded set
LOCAL_ONLY_TYPES = {'peer', 'transit_key', 'group_key', 'transit_prekey',
                    'group_prekey', 'recorded', 'network_joined', 'invite_accepted',
                    'sync_connect', 'purge_expired'}

should_mark_shareable = event_type not in LOCAL_ONLY_TYPES

# Line ~430 - another hardcoded set
EPHEMERAL_TYPES = {'sync_connect', 'sync_request', 'sync_response', 'purge_expired'}
if event_type in EPHEMERAL_TYPES:
    log.warning(f"[EPHEMERAL_DROP] Dropping ephemeral {event_type}...")
    return [None, recorded_id]

# Lines 464-566 - massive if/elif chain
if event_type == 'message':
    projected_id = message.project(ref_id, recorded_by, recorded_at, db)
elif event_type == 'message_deletion':
    from events.content import message_deletion
    projected_id = message_deletion.project(ref_id, recorded_by, recorded_at, db)
elif event_type == 'group':
    projected_id = group.project(ref_id, recorded_by, recorded_at, db)
elif event_type == 'peer':
    peer.project(ref_id, recorded_by, db)
    projected_id = ref_id
elif event_type == 'transit_key':
    # ... 25+ more elif branches
```

## After (Using Registry)

```python
from events import registry

# Shareable check - one line
should_mark_shareable = registry.is_shareable(event_type)

# Ephemeral check - one line
if registry.is_ephemeral(event_type):
    log.warning(f"[EPHEMERAL_DROP] Dropping ephemeral {event_type}...")
    return [None, recorded_id]

# Dispatch - dynamic lookup
project_fn = registry.get_project_fn(event_type)
if project_fn:
    # Handle varying signatures
    import inspect
    sig = inspect.signature(project_fn)
    params = list(sig.parameters.keys())

    if 'event_data' in params:
        # file_slice, message_attachment style
        projected_id = project_fn(ref_id, event_data, recorded_by, recorded_at, db)
    elif 'recorded_at' in params:
        # Standard style
        projected_id = project_fn(ref_id, recorded_by, recorded_at, db)
    else:
        # Minimal style (peer, transit_key)
        project_fn(ref_id, recorded_by, db)
        projected_id = ref_id
else:
    log.warning(f"No project function for event type: {event_type}")
```

## TABLE_MAP Replacement

```python
# Before - hardcoded dict
TABLE_MAP = {
    'channel': ('channels', 'channel_id'),
    'group': ('groups', 'group_id'),
    'peer_shared': ('peers_shared', 'peer_shared_id'),
    # ... many more entries
}

# After - from registry
table_info = registry.get_projection_table(event_type)
if table_info:
    table, id_col = table_info
    # ... query
```

## Key Benefits

1. **Adding new event type**: Just add metadata to the module file
2. **No forgotten updates**: Registry discovers automatically
3. **Safe default**: Unknown types = not shareable
4. **Easy audit**: `grep -r "SHAREABLE = True" events/`
5. **Single source of truth**: Metadata lives with the code it describes
