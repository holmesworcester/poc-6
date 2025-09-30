"""Reflect handler - executes event-triggered reflector functions."""

import sqlite3
import time
from typing import Dict, List, Any, Callable
from core.handlers import Handler


class ReflectHandler(Handler):
    """Executes reflector functions in response to events."""

    @property
    def name(self) -> str:
        return "reflect"

    def __init__(self) -> None:
        super().__init__()
        self.reflectors: Dict[str, Callable[[Dict[str, Any], sqlite3.Connection, int], tuple[bool, List[Dict[str, Any]]]]] = self._load_reflectors()

    def _load_reflectors(self) -> Dict[str, Callable[[Dict[str, Any], sqlite3.Connection, int], tuple[bool, List[Dict[str, Any]]]]]:
        """Dynamically load all reflector functions from event directories."""
        import os
        import importlib
        from pathlib import Path

        reflectors: Dict[str, Callable[[Dict[str, Any], sqlite3.Connection, int], tuple[bool, List[Dict[str, Any]]]]] = {}

        # First, try protocol-level reflectors module (preferred)
        try:
            import importlib
            proto_mod = importlib.import_module('protocols.quiet.reflectors')
            mapping = getattr(proto_mod, 'REFLECTORS', {})
            if isinstance(mapping, dict):
                reflectors.update(mapping)
        except Exception:
            pass

        # Then, fallback to scanning event directories for legacy reflectors
        # Find the events directory
        events_dir = Path(__file__).parent.parent / 'events'

        if not events_dir.exists():
            return reflectors

        # Scan each event type directory for reflector.py
        for event_dir in events_dir.iterdir():
            if not event_dir.is_dir():
                continue

            reflector_file = event_dir / 'reflector.py'
            if not reflector_file.exists():
                continue

            # Import the reflector module
            event_type = event_dir.name
            module_name = f'protocols.quiet.events.{event_type}.reflector'

            try:
                module = importlib.import_module(module_name)

                # Look for functions that end with _reflector
                for attr_name in dir(module):
                    if attr_name.endswith('_reflector') and not attr_name.startswith('_'):
                        reflector_fn = getattr(module, attr_name)
                        if callable(reflector_fn):
                            # Map the event type to the reflector
                            reflectors[event_type] = reflector_fn
                            # print(f"[ReflectHandler] Loaded reflector: {event_type} -> {attr_name}")
            except Exception as e:
                print(f"[ReflectHandler] Failed to load reflector from {module_name}: {e}")

        return reflectors

    def filter(self, envelope: Dict[str, Any]) -> bool:
        """Process validated events that have reflectors."""
        event_type = envelope.get('event_type')
        # Only process incoming, validated events (not our outgoing ones)
        return (
            (event_type in self.reflectors)
            and bool(envelope.get('validated'))
            and (not bool(envelope.get('is_outgoing')))
            and bool(envelope.get('event_plaintext'))
        )

    def process(self, envelope: Dict[str, Any], db: sqlite3.Connection) -> List[Dict[str, Any]]:
        """Execute the appropriate reflector."""
        event_type = envelope['event_type']
        reflector_fn = self.reflectors[event_type]
        time_now_ms = int(time.time() * 1000)


        # Run the reflector (reflectors get read-only access)
        try:
            success, envelopes = reflector_fn(envelope, db, time_now_ms)
        except Exception as e:
            print(f"[ReflectHandler] Reflector for {event_type} failed: {e}")
            return []

        if success:
            print(f"[ReflectHandler] Reflector for {event_type} succeeded, emitting {len(envelopes)} envelopes")
            return envelopes
        else:
            print(f"[ReflectHandler] Reflector for {event_type} returned failure")
            return []
