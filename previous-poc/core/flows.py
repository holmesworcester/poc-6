"""
Core flow helpers for readable orchestrations.

Flows should:
- Query via query registry (read-only)
- Emit events via the pipeline runner (no commands registry)
- Return a result dict

They must not write to the DB directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, List, Tuple
import sqlite3

from core.db import ReadOnlyConnection


@dataclass
class FlowCtx:
    db: sqlite3.Connection
    runner: Any
    protocol_dir: str
    request_id: str
    actor_peer_id: str | None = None

    @staticmethod
    def from_params(params: Dict[str, Any]) -> "FlowCtx":
        db = params.get('_db')
        runner = params.get('_runner')
        protocol_dir = params.get('_protocol_dir')
        request_id = params.get('_request_id')
        # Enforce actor normalization universally (protocol-specific)
        from pathlib import Path as _Path
        import importlib as _importlib
        proto_name = _Path(protocol_dir).name if protocol_dir else 'quiet'
        api_mod = _importlib.import_module(f'protocols.{proto_name}.api')
        p = api_mod.normalize_actor(params.get('_op_id') or 'flow', params)
        if not (db and runner and protocol_dir and request_id):
            raise ValueError("FlowCtx missing required context (_db, _runner, _protocol_dir, _request_id)")
        actor = p.get('peer_id') or p.get('identity_id')
        return FlowCtx(db=db, runner=runner, protocol_dir=str(protocol_dir), request_id=str(request_id), actor_peer_id=str(actor) if isinstance(actor, str) else None)

    def query(self, query_id: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return query(self, query_id, params)

    def emit_envelope(self, envelope: Dict[str, Any]) -> Dict[str, str]:
        """Emit a fully-formed envelope through the pipeline as-is.

        This lets protocol flows construct arbitrary envelopes (including
        protocol-specific fields) without expanding core.

        Returns mapping of event_type -> event_id for stored events.
        """
        if not isinstance(envelope, dict):
            raise TypeError("emit_envelope expects a dict envelope")
        env = dict(envelope)
        env.setdefault('request_id', self.request_id)
        return self.runner.run(protocol_dir=self.protocol_dir, input_envelopes=[env], db=self.db)


    # Preferred: emit a fully-formed envelope (protocol defines internals)
    def emit_event(self, envelope: Dict[str, Any]) -> str:
        if not isinstance(envelope, dict):
            raise TypeError("emit_event expects an envelope dict")
        env: Dict[str, Any] = dict(envelope)
        env.setdefault('request_id', self.request_id)
        ids = self.runner.run(protocol_dir=self.protocol_dir, input_envelopes=[env], db=self.db)
        etype = env.get('event_type') or (env.get('event_plaintext') or {}).get('type')
        if etype == 'user':
            print(f"[FlowCtx.emit_event] Got IDs from runner: {ids}, looking for event_type={etype}")
        if isinstance(etype, str) and etype in ids:
            return ids[etype]
        if len(ids) == 1:
            return next(iter(ids.values()))
        return ids.get(etype, '')


def query(ctx: FlowCtx, query_id: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """
    Execute a read-only query via query registry.
    """
    from core.queries import query_registry
    return query_registry.execute(query_id, params or {}, ReadOnlyConnection(ctx.db))




class FlowRegistry:
    """Registry for API flows (operation-like functions)."""

    def __init__(self) -> None:
        self._flows: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}

    def register(self, op_id: str, func: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self._flows[op_id] = func

    def has_flow(self, op_id: str) -> bool:
        return op_id in self._flows

    def execute(self, op_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if op_id not in self._flows:
            raise ValueError(f"Unknown flow op: {op_id}")
        # Enforce actor normalization for flows invoked outside of API client (protocol-specific)
        import importlib as _importlib
        from pathlib import Path as _Path
        proto_name = 'quiet'
        if isinstance(params, dict) and params.get('_protocol_dir'):
            try:
                proto_name = _Path(str(params.get('_protocol_dir'))).name
            except Exception:
                pass
        api_mod = _importlib.import_module(f'protocols.{proto_name}.api')
        p = api_mod.normalize_actor(op_id, params)
        # Pass op_id through for FlowCtx visibility
        p['_op_id'] = op_id
        return self._flows[op_id](p)

    def list_flows(self) -> List[str]:
        return sorted(self._flows.keys())

    def alias(self, alias_id: str, target_id: str) -> None:
        """Register an alias for an existing flow operation."""
        if target_id not in self._flows:
            # Allow aliasing to be deferred: it can be re-applied later if needed
            # For now, raise to surface missing target during discovery
            raise ValueError(f"Cannot alias '{alias_id}' to missing flow '{target_id}'")
        self._flows[alias_id] = self._flows[target_id]


flows_registry = FlowRegistry()


def flow_op(op_id: Optional[str] = None) -> Callable[[Callable[[Dict[str, Any]], Dict[str, Any]]], Callable[[Dict[str, Any]], Dict[str, Any]]]:
    """
    Decorator to register a flow function as an API operation.

    If op_id is not provided, derive it as '<event>.<func_name>' from the module path
    'protocols.<protocol>.events.<event>.flows'.
    """
    def decorator(func: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        nonlocal op_id
        if op_id is None:
            parts = func.__module__.split('.')
            # Expect: ['protocols', '<protocol>', 'events', '<event>', 'flows']
            event = parts[3] if len(parts) >= 5 else func.__name__
            op_id = f"{event}.{func.__name__}"
        flows_registry.register(op_id, func)
        return func

    return decorator
