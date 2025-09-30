"""
Explicit API surface and auth/actor normalization for the Quiet protocol.

This file declares which operations are exposed and how they are fulfilled:
- 'flow'   => a registered @flow_op function
- 'query'  => a query in query registry

Only operations listed here (plus 'core.*') are callable via API.

It also contains protocol-specific actor normalization previously in core.auth.
"""

from __future__ import annotations

from typing import Any, Dict

EXPOSED: dict[str, str] = {
    # Flows
    'user.join_as_user': 'flow',
    'network.create': 'flow',
    'network.create_as_user': 'flow',
    'network.tick': 'flow',
    'group.create': 'flow',
    'channel.create': 'flow',
    'message.create': 'flow',
    'invite.create': 'flow',
    'address.announce': 'flow',
    'key_secret.create': 'flow',
    'peer_secret.create': 'flow',
    'sync_request.run': 'flow',
    'job.run': 'flow',
    'user.create': 'flow',
    # Utility tick (no actor)
    'network.tick': 'flow',
    # Removed legacy identity.* flows; use peer_secret.*

    # Queries
    'message.get': 'query',
    'user.get': 'query',
    'peer_secret.list': 'query',
}

# Aliases for backward compatibility during migration
ALIASES: dict[str, str] = {}


# Protocol-specific: bootstrap ops allowed without peer context
BOOTSTRAP_ALLOWLIST = {
    'network.create_as_user',
    'user.join_as_user',
    # Local-only peer bootstrap
    'peer_secret.create',
    # Jobs / utility flows that do not require an actor
    'sync_request.run',
    'job.run',
    'network.tick',
}


def normalize_actor(operation_id: str, params: Dict[str, Any] | None) -> Dict[str, Any]:
    """Enforce and normalize actor for non-system operations.

    - Requires `peer_id` or `peer_secret_id` for any non-system op not in BOOTSTRAP_ALLOWLIST.
    - Returns a shallow-copied params dict that includes both `peer_id` and `peer_secret_id`.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    # For non-system, non-allowlisted ops, require explicit peer_secret_id as actor
    if not operation_id.startswith('system.') and operation_id not in BOOTSTRAP_ALLOWLIST:
        p = dict(params)
        actor_secret = p.get('peer_secret_id')
        if not isinstance(actor_secret, str) or not actor_secret:
            raise ValueError("peer_secret_id is required for this operation")
        # Do NOT mirror between peer_id and peer_secret_id; flows must pass peer_id explicitly when needed
        return p
    return dict(params)
