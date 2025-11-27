# Event Registry Design

**Goal**: Reduce brittleness when adding new event types. Currently requires updating:
1. `LOCAL_ONLY_TYPES` set in recorded.py
2. `EPHEMERAL_TYPES` set in recorded.py
3. `TABLE_MAP` dict in recorded.py
4. The massive if/elif dispatch chain (~30 branches) in recorded.py

**Key constraint**: Make it hard to accidentally make things shareable that should NOT be shared.

## Current Problems

### 1. Easy to Forget
Adding a new event type like `foo` requires:
```python
# recorded.py line 352 - must add if NOT shareable
LOCAL_ONLY_TYPES = {'peer', 'transit_key', ...}

# recorded.py line 430 - must add if ephemeral
EPHEMERAL_TYPES = {'sync_connect', 'sync_request', ...}

# recorded.py line ~500 - must add dispatch
elif event_type == 'foo':
    from events.wherever import foo
    projected_id = foo.project(ref_id, recorded_by, recorded_at, db)
```

If you forget `LOCAL_ONLY_TYPES`, your event accidentally becomes shareable (bad!).

### 2. Information Scattered
To understand if an event type is shareable, you must:
1. Search for it in LOCAL_ONLY_TYPES (absence = shareable)
2. Check if it's in EPHEMERAL_TYPES
3. Find its project dispatch
4. Find its module location

## Design Options

### Option A: Module-level Metadata (Recommended)

Each event module declares its own properties:

```python
# events/identity/peer.py
EVENT_TYPE = 'peer'
SHAREABLE = False  # Must explicitly opt-in to sharing
EPHEMERAL = False
PROJECTION_TABLE = None  # or ('peers', 'peer_id')

def project(event_id: str, recorded_by: str, db: Any) -> None:
    ...
```

Registry auto-discovers all event modules:

```python
# events/registry.py
import importlib
import pkgutil
from pathlib import Path

_registry: dict[str, dict] = {}

def _discover_events():
    """Auto-discover all event modules under events/."""
    events_path = Path(__file__).parent

    for subdir in ['identity', 'content', 'group', 'network']:
        subdir_path = events_path / subdir
        for module_info in pkgutil.iter_modules([str(subdir_path)]):
            if module_info.name.startswith('_'):
                continue
            module = importlib.import_module(f'events.{subdir}.{module_info.name}')

            if hasattr(module, 'EVENT_TYPE'):
                event_type = module.EVENT_TYPE
                _registry[event_type] = {
                    'module': module,
                    'shareable': getattr(module, 'SHAREABLE', None),  # None = error
                    'ephemeral': getattr(module, 'EPHEMERAL', False),
                    'projection_table': getattr(module, 'PROJECTION_TABLE', None),
                }

def get_project_fn(event_type: str):
    """Get project function for event type."""
    if event_type not in _registry:
        return None
    return _registry[event_type]['module'].project

def is_shareable(event_type: str) -> bool:
    """Check if event type is shareable."""
    if event_type not in _registry:
        raise ValueError(f"Unknown event type: {event_type}")
    shareable = _registry[event_type]['shareable']
    if shareable is None:
        raise ValueError(f"Event type {event_type} missing SHAREABLE declaration")
    return shareable

def is_ephemeral(event_type: str) -> bool:
    """Check if event type is ephemeral."""
    return _registry.get(event_type, {}).get('ephemeral', False)

# Auto-discover on import
_discover_events()
```

**Pros**:
- Metadata lives with the code it describes
- Adding new event type: just create module with proper declarations
- SHAREABLE=None (missing) raises error = fail-safe
- Easy to audit: `grep -r "SHAREABLE = True" events/`

**Cons**:
- Requires updating all existing modules
- Auto-import at startup could have side effects
- Module naming must match event type

### Option B: Centralized Registry File

Single source of truth, but explicit registration:

```python
# events/registry.py
EVENTS = {
    'peer': {
        'module': 'events.identity.peer',
        'shareable': False,
        'ephemeral': False,
    },
    'user': {
        'module': 'events.identity.user',
        'shareable': True,
        'ephemeral': False,
        'projection_table': ('users', 'user_id'),
    },
    # ... all event types
}
```

**Pros**:
- Single place to see all event types and their properties
- Easy to review shareability at a glance
- No auto-discovery magic

**Cons**:
- Still requires manual registration (but in ONE place)
- Module path can get out of sync with actual file
- Must remember to update when adding new types

### Option C: Decorator-based Registration

```python
# events/registry.py
_registry = {}

def event(event_type: str, shareable: bool, ephemeral: bool = False):
    def decorator(cls_or_module):
        _registry[event_type] = {
            'project': cls_or_module.project,
            'shareable': shareable,
            'ephemeral': ephemeral,
        }
        return cls_or_module
    return decorator

# events/identity/peer.py
from events.registry import event

@event('peer', shareable=False)
class PeerEvent:
    @staticmethod
    def project(event_id: str, recorded_by: str, db: Any) -> None:
        ...

# OR function-based:
@event('peer', shareable=False)
def module_marker(): pass

def project(...): ...
```

**Pros**:
- Explicit registration
- `shareable` is required parameter (fail-safe)
- Registration happens on import

**Cons**:
- Requires importing all event modules to register
- Decorator pattern is unfamiliar for this codebase
- Still need explicit import somewhere

### Option D: Directory-based Convention

Event type derived from file path:

```
events/
  shareable/           # Everything here is shareable
    identity/
      user.py         # type='user'
      peer_shared.py  # type='peer_shared'
    content/
      message.py      # type='message'
  local/               # Everything here is local-only
    identity/
      peer.py         # type='peer'
    network/
      transit_key.py  # type='transit_key'
  ephemeral/           # Local AND ephemeral (dropped if deps missing)
    network/
      sync_connect.py # type='sync_connect'
```

Auto-discover:
```python
for category in ['shareable', 'local', 'ephemeral']:
    for subdir in os.listdir(f'events/{category}'):
        for filename in os.listdir(f'events/{category}/{subdir}'):
            event_type = filename.replace('.py', '')
            _registry[event_type] = {
                'shareable': category == 'shareable',
                'ephemeral': category == 'ephemeral',
                ...
            }
```

**Pros**:
- File location IS the declaration
- Impossible to forget shareability (must choose dir)
- Visual structure shows security model
- Moving file changes behavior (explicit action)

**Cons**:
- Major refactor of file structure
- Imports change everywhere
- Existing tools/scripts may break

## Recommendation

**Option A (Module-level Metadata)** with a twist:

1. **Default to NOT shareable** - if SHAREABLE is missing, default to False (safe default)
2. **Require explicit opt-in** for shareable events
3. **Add CI check** that all event modules have SHAREABLE declared

```python
# Migration: Add to each module
EVENT_TYPE = 'peer'
SHAREABLE = False  # Default safe

# For shareable events - explicit opt-in
EVENT_TYPE = 'user'
SHAREABLE = True  # Explicitly sharing

# Registry: Safe default
def is_shareable(event_type: str) -> bool:
    if event_type not in _registry:
        return False  # Unknown = don't share
    return _registry[event_type].get('shareable', False)  # Missing = don't share
```

## Migration Plan

1. Create `events/registry.py` with auto-discovery
2. Add `EVENT_TYPE`, `SHAREABLE` to one module as proof of concept
3. Update `recorded.py` to use registry for dispatch and shareability checks
4. Gradually add metadata to remaining modules
5. Remove hardcoded `LOCAL_ONLY_TYPES` and dispatch chain
6. Add CI lint to require `SHAREABLE` declaration

## Visibility: Easy Audit

After migration:
```bash
# See all shareable events
grep -r "SHAREABLE = True" events/

# See all local-only events
grep -r "SHAREABLE = False" events/

# Find events missing declaration (should be none)
for f in events/*/*.py; do
  if ! grep -q "SHAREABLE" "$f" && grep -q "EVENT_TYPE" "$f"; then
    echo "Missing SHAREABLE: $f"
  fi
done
```
