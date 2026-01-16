"""Event type registry with auto-discovery.

Each event module should declare:
- EVENT_TYPE: str - the event type name (e.g., 'user', 'message')
- SHAREABLE: bool - whether this event should be synced to other peers
- EPHEMERAL: bool (optional) - whether to drop if deps missing (default False)
- PROJECTION_TABLE: tuple[str, str] | None (optional) - (table_name, id_column) for created_at lookup

Example event module:
    EVENT_TYPE = 'user'
    SHAREABLE = True
    EPHEMERAL = False
    PROJECTION_TABLE = ('users', 'user_id')

    def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
        ...

Safety: SHAREABLE defaults to False if missing (safe default).
"""
import importlib
import pkgutil
import logging
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# Registry: event_type -> metadata dict
_registry: dict[str, dict[str, Any]] = {}

# Track if discovery has run
_discovered = False


def _discover_events() -> None:
    """Auto-discover all event modules under events/.

    Scans events/{identity,content,group,network}/*.py for modules
    that declare EVENT_TYPE.
    """
    global _discovered
    if _discovered:
        return

    events_path = Path(__file__).parent
    subdirs = ['identity', 'content', 'group', 'network']

    for subdir in subdirs:
        subdir_path = events_path / subdir
        if not subdir_path.exists():
            continue

        for module_info in pkgutil.iter_modules([str(subdir_path)]):
            if module_info.name.startswith('_'):
                continue

            module_name = f'events.{subdir}.{module_info.name}'
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                log.warning(f"Failed to import {module_name}: {e}")
                continue

            # Only register modules that declare EVENT_TYPE
            if not hasattr(module, 'EVENT_TYPE'):
                continue

            event_type = module.EVENT_TYPE
            if event_type in _registry:
                log.warning(f"Duplicate EVENT_TYPE '{event_type}' in {module_name}, "
                           f"already registered from {_registry[event_type]['module_name']}")
                continue

            _registry[event_type] = {
                'module': module,
                'module_name': module_name,
                'shareable': getattr(module, 'SHAREABLE', False),  # Safe default
                'ephemeral': getattr(module, 'EPHEMERAL', False),
                'projection_table': getattr(module, 'PROJECTION_TABLE', None),
                'event_spec': getattr(module, 'EVENT_SPEC', None),
                'project_pure': getattr(module, 'project_pure', None),
            }

            log.debug(f"Registered event type '{event_type}' from {module_name}")

    _discovered = True
    log.info(f"Event registry: discovered {len(_registry)} event types")


def get_project_fn(event_type: str) -> Callable | None:
    """Get project function for an event type.

    Args:
        event_type: The event type (e.g., 'user', 'message')

    Returns:
        The project function, or None if not registered
    """
    _discover_events()

    if event_type not in _registry:
        return None

    module = _registry[event_type]['module']
    if not hasattr(module, 'project'):
        log.warning(f"Event type '{event_type}' has no project function")
        return None

    return module.project


def get_project_pure_fn(event_type: str) -> Callable | None:
    """Get pure projector function for an event type, if available."""
    _discover_events()
    if event_type not in _registry:
        return None
    return _registry[event_type].get('project_pure')


def get_event_spec(event_type: str) -> dict[str, Any] | None:
    """Get EVENT_SPEC for an event type, if available."""
    _discover_events()
    if event_type not in _registry:
        return None
    return _registry[event_type].get('event_spec')


def is_shareable(event_type: str) -> bool:
    """Check if an event type is shareable (should sync to other peers).

    Unknown event types return False (safe default).

    Args:
        event_type: The event type

    Returns:
        True if shareable, False otherwise
    """
    _discover_events()

    if event_type not in _registry:
        # Unknown type = don't share (safe default)
        return False

    return _registry[event_type]['shareable']


def is_ephemeral(event_type: str) -> bool:
    """Check if an event type is ephemeral (drop if deps missing).

    Ephemeral events are transit-layer events that can be retried.

    Args:
        event_type: The event type

    Returns:
        True if ephemeral, False otherwise
    """
    _discover_events()
    return _registry.get(event_type, {}).get('ephemeral', False)


def get_projection_table(event_type: str) -> tuple[str, str] | None:
    """Get projection table info for an event type.

    Returns the table name and ID column for events that project to database tables.
    Used for looking up created_at or other projection-specific data.

    Args:
        event_type: The event type

    Returns:
        (table_name, id_column) tuple, or None if no projection table
    """
    _discover_events()
    return _registry.get(event_type, {}).get('projection_table')


def get_registered_types() -> list[str]:
    """Get all registered event types."""
    _discover_events()
    return list(_registry.keys())


def get_shareable_types() -> set[str]:
    """Get set of shareable event types."""
    _discover_events()
    return {t for t, meta in _registry.items() if meta['shareable']}


def get_local_only_types() -> set[str]:
    """Get set of local-only (non-shareable) event types."""
    _discover_events()
    return {t for t, meta in _registry.items() if not meta['shareable']}


def get_ephemeral_types() -> set[str]:
    """Get set of ephemeral event types."""
    _discover_events()
    return {t for t, meta in _registry.items() if meta['ephemeral']}


# Convenience: print registry summary
def print_registry() -> None:
    """Print a summary of registered event types."""
    _discover_events()

    print("\n=== Event Registry ===")
    print(f"Total event types: {len(_registry)}")

    shareable = [t for t, m in _registry.items() if m['shareable']]
    local_only = [t for t, m in _registry.items() if not m['shareable']]
    ephemeral = [t for t, m in _registry.items() if m['ephemeral']]

    print(f"\nShareable ({len(shareable)}):")
    for t in sorted(shareable):
        print(f"  - {t}")

    print(f"\nLocal-only ({len(local_only)}):")
    for t in sorted(local_only):
        print(f"  - {t}")

    print(f"\nEphemeral ({len(ephemeral)}):")
    for t in sorted(ephemeral):
        print(f"  - {t}")
