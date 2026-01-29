"""Event type registry with auto-discovery.

Each event module should declare:
- EVENT_TYPE: str - the event type name (e.g., 'user', 'message')
- SHAREABLE: bool - whether this event should be synced to other peers
- PROJECTION_TABLE: tuple[str, str] | None (optional) - (table_name, id_column) for created_at lookup

Example event module:
    EVENT_TYPE = 'user'
    SHAREABLE = True
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

# Wire format type code -> event_type mapping
_type_code_to_event_type: dict[int, str] = {}

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

            # Get wire format metadata if available
            wire_type_code = getattr(module, 'WIRE_TYPE_CODE', None)
            decode_wire_event = getattr(module, 'decode_wire_event', None)
            encode_wire_event = getattr(module, 'encode_wire_event', None)

            _registry[event_type] = {
                'module': module,
                'module_name': module_name,
                'shareable': getattr(module, 'SHAREABLE', False),  # Safe default
                'projection_table': getattr(module, 'PROJECTION_TABLE', None),
                'event_spec': getattr(module, 'EVENT_SPEC', None),
                'project_pure': getattr(module, 'project_pure', None),
                'wire_type_code': wire_type_code,
                'decode_wire_event': decode_wire_event,
                'encode_wire_event': encode_wire_event,
            }

            # Register wire type code -> event_type mapping
            if wire_type_code is not None:
                if wire_type_code in _type_code_to_event_type:
                    existing = _type_code_to_event_type[wire_type_code]
                    log.warning(f"Duplicate WIRE_TYPE_CODE 0x{wire_type_code:02x} for '{event_type}', "
                               f"already registered for '{existing}'")
                else:
                    _type_code_to_event_type[wire_type_code] = event_type

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


# Convenience: print registry summary
def print_registry() -> None:
    """Print a summary of registered event types."""
    _discover_events()

    print("\n=== Event Registry ===")
    print(f"Total event types: {len(_registry)}")

    shareable = [t for t, m in _registry.items() if m['shareable']]
    local_only = [t for t, m in _registry.items() if not m['shareable']]

    print(f"\nShareable ({len(shareable)}):")
    for t in sorted(shareable):
        print(f"  - {t}")

    print(f"\nLocal-only ({len(local_only)}):")
    for t in sorted(local_only):
        print(f"  - {t}")


# Wire format registry functions

def get_event_type_by_code(type_code: int) -> str | None:
    """Get event type name by wire format type code.

    Args:
        type_code: Wire format type code (e.g., 0x01 for message)

    Returns:
        Event type string, or None if not registered
    """
    _discover_events()
    return _type_code_to_event_type.get(type_code)


def get_wire_decoder(event_type: str) -> Callable | None:
    """Get decode_wire_event function for an event type.

    Args:
        event_type: The event type

    Returns:
        The decode_wire_event function, or None if not available
    """
    _discover_events()
    if event_type not in _registry:
        return None
    return _registry[event_type].get('decode_wire_event')


def get_wire_encoder(event_type: str) -> Callable | None:
    """Get encode_wire_event function for an event type.

    Args:
        event_type: The event type

    Returns:
        The encode_wire_event function, or None if not available
    """
    _discover_events()
    if event_type not in _registry:
        return None
    return _registry[event_type].get('encode_wire_event')


def get_wire_type_code(event_type: str) -> int | None:
    """Get wire format type code for an event type.

    Args:
        event_type: The event type

    Returns:
        Wire type code, or None if not available
    """
    _discover_events()
    if event_type not in _registry:
        return None
    return _registry[event_type].get('wire_type_code')


def decode_by_type_code(
    type_code: int,
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode wire event by type code using registry.

    Args:
        type_code: Wire format type code
        data: Wire envelope bytes
        recorded_by: Peer ID for decryption
        db: Database connection
        key_cache: Optional key cache

    Returns:
        (event_data, missing_key_ids) tuple
    """
    _discover_events()

    event_type = _type_code_to_event_type.get(type_code)
    if not event_type:
        return None, []

    decoder = _registry[event_type].get('decode_wire_event')
    if not decoder:
        return None, []

    # Call decoder - some decoders take (data,), others take (data, recorded_by, db, key_cache)
    import inspect
    sig = inspect.signature(decoder)
    params = list(sig.parameters.keys())

    if len(params) == 1:
        # Simple decoder: decode_wire_event(data) -> dict
        result = decoder(data)
        if isinstance(result, dict):
            return result, []
        return result, []
    elif len(params) >= 3:
        # Complex decoder: decode_wire_event(data, recorded_by, db, key_cache=None) -> (dict, missing_keys)
        result = decoder(data, recorded_by, db, key_cache=key_cache)
        if isinstance(result, tuple):
            return result
        return result, []
    else:
        log.warning(f"Unexpected decoder signature for {event_type}: {params}")
        return None, []


def get_registered_wire_types() -> dict[int, str]:
    """Get mapping of wire type codes to event types."""
    _discover_events()
    return dict(_type_code_to_event_type)
