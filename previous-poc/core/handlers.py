"""
Handler base class and registry for pipeline processing.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Callable, Optional
import sqlite3
from typing import Any


class Handler(ABC):
    """Base class for all handlers in the pipeline."""
    
    @abstractmethod
    def filter(self, envelope: dict[str, Any]) -> bool:
        """
        Return True if this handler should process the envelope.
        This is how handlers subscribe to specific envelope traits.
        """
        pass
    
    @abstractmethod
    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """
        Process the envelope and emit zero or more new envelopes.
        Can modify the database as needed.
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Handler name for logging/debugging."""
        pass


class HandlerRegistry:
    """Registry for all handlers in the system."""
    
    def __init__(self) -> None:
        self._handlers: List[Handler] = []
        self._handler_map: Dict[str, Handler] = {}
    
    def register(self, handler: Handler) -> None:
        """Register a handler."""
        self._handlers.append(handler)
        self._handler_map[handler.name] = handler
    
    def process_envelope(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """
        Pass envelope through all matching handlers.
        Returns all new envelopes emitted by handlers.
        """
        if not isinstance(envelope, dict):
            print(f"[registry] ERROR: process_envelope got {type(envelope)} instead of dict")
            return []

        all_emitted: List[dict[str, Any]] = []

        for handler in self._handlers:
            # Global rule: do not process envelopes carrying an error
            if envelope.get('error'):
                break
            if handler.filter(envelope):
                print(f"[{handler.name}] Processing: {envelope}")
                emitted = handler.process(envelope, db)
                if emitted:
                    print(f"[{handler.name}] Emitted {len(emitted)} envelopes")
                    all_emitted.extend(emitted)

        return all_emitted
    
    def get_handler(self, name: str) -> Optional[Handler]:
        """Get a handler by name."""
        return self._handler_map.get(name)


# Global registry instance
registry = HandlerRegistry()
