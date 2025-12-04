"""Sync tests - isolated testing of sync protocols.

This test category provides:
- Stubbed connection interface (bypasses encryption/handshakes)
- Event factory for generating test data (IDs + timestamps only)
- Rich logging and visibility into sync state
- Works with any sync protocol using the connection interface

Unlike scenario tests which test the full stack, sync tests isolate
the sync protocol logic for easier debugging and faster iteration.
"""

from .stub_connection import StubConnectionBridge, StubPeer, stub_bridge
from .event_factory import EventFactory, EventSet
from .sync_harness import SyncTestHarness

__all__ = [
    'StubConnectionBridge',
    'StubPeer',
    'stub_bridge',
    'EventFactory',
    'EventSet',
    'SyncTestHarness',
]
