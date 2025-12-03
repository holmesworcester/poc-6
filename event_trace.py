"""Event tracing for human-readable event logging.

This module provides a simple mechanism for event modules to record
what events they create with human-readable context. The CLI or tests
can start a trace, run operations, then collect the recorded events.

Usage:
    from event_trace import trace

    # In event create() functions:
    trace('message', channel='#general', author='alice', content='hello...')

    # In CLI/tests:
    import event_trace
    event_trace.start()
    # ... run operations ...
    events = event_trace.collect()  # [{'type': 'message', 'channel': '#general', ...}]
"""

from typing import Any
from contextvars import ContextVar

# Context variable for thread-safe trace collection
_trace: ContextVar[list | None] = ContextVar('event_trace', default=None)


def start():
    """Start collecting events. Call before operations you want to trace."""
    _trace.set([])


def trace(event_type: str, **fields):
    """Record an event. No-op if tracing not started.

    Call this from event create() functions with human-readable field values.
    Use angle brackets for referenced entities: channel='<#general>', author='<alice>'
    """
    current = _trace.get()
    if current is not None:
        current.append({'type': event_type, **fields})


def collect() -> list[dict[str, Any]]:
    """Collect and clear recorded events. Returns empty list if tracing not started."""
    current = _trace.get()
    _trace.set(None)
    return current or []


def render(events: list[dict[str, Any]]) -> list[str]:
    """Render events as human-readable strings in JSON-lite format.

    Returns list of strings like:
        '  -> message {channel=<#general>, author=<alice>, content="hello"}'
    """
    lines = []
    for event in events:
        event_type = event.get('type', '???')
        fields = {k: v for k, v in event.items() if k != 'type'}
        field_strs = [f"{k}={v}" for k, v in fields.items()]
        lines.append(f"  -> {event_type} {{{', '.join(field_strs)}}}")
    return lines
