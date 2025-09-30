"""
Queries for prekey selection and lookup.
"""
from __future__ import annotations

from typing import Any, Optional, Dict
from core.db import ReadOnlyConnection
from core.queries import query


@query
def get_active_for_peer(db: ReadOnlyConnection, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Select an active prekey for a peer in a network using simple criteria.

    Params:
    - peer_id: recipient peer/identity id
    - network_id: target network id
    - min_expires_ms (optional): prefer keys with expires_at >= now + min_expires_ms
    - prefer_newest (optional, default True): order by created_at desc

    Returns: {prekey_id, public_key, created_at, expires_at} or None
    """
    peer_id = params['peer_id']
    network_id = params['network_id']
    min_expires = int(params.get('min_expires_ms', 0))
    prefer_newest = bool(params.get('prefer_newest', True))

    order_clause = 'ORDER BY created_at DESC' if prefer_newest else 'ORDER BY created_at ASC'

    sql = f"""
        SELECT prekey_id, public_key, created_at, expires_at
        FROM prekeys
        WHERE peer_id = ? AND network_id = ? AND active = 1
        {order_clause}
        LIMIT 5
    """
    rows = db.execute(sql, (peer_id, network_id)).fetchall()
    if not rows:
        return None

    if min_expires > 0:
        for r in rows:
            if r['expires_at'] is None or int(r['expires_at']) >= min_expires:
                return dict(r)
        # fallback to first if none meet TTL
        return dict(rows[0])

    return dict(rows[0])
