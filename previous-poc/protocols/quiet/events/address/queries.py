"""
Address queries (read-only) for flows.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from core.queries import query
from core.db import ReadOnlyConnection


@query
def get_latest_for_peer(db: ReadOnlyConnection, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Return the most recently registered active address for a given peer in a network.

    Params:
    - peer_id: str (required)
    - network_id: str (optional; if provided, filter by network)
    """
    peer_id = params.get('peer_id')
    network_id = params.get('network_id')
    if not isinstance(peer_id, str) or not peer_id:
        return None
    if isinstance(network_id, str) and network_id:
        row = db.execute(
            """
            SELECT ip, port
            FROM addresses
            WHERE peer_id = ? AND network_id = ? AND is_active = TRUE
            ORDER BY registered_at_ms DESC
            LIMIT 1
            """,
            (peer_id, network_id),
        ).fetchone()
    else:
        row = db.execute(
            """
            SELECT ip, port
            FROM addresses
            WHERE peer_id = ? AND is_active = TRUE
            ORDER BY registered_at_ms DESC
            LIMIT 1
            """,
            (peer_id,),
        ).fetchone()
    if not row:
        return None
    return {'ip': row['ip'], 'port': row['port']}


@query
def list_active(db: ReadOnlyConnection, params: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return all active (ip, port) endpoints.

    Optional:
    - network_id: filter by network
    - peer_id: filter by peer
    """
    network_id = params.get('network_id')
    peer_id = params.get('peer_id')
    sql = "SELECT ip, port FROM addresses WHERE is_active = TRUE"
    args: list[Any] = []
    if peer_id:
        sql += " AND peer_id = ?"
        args.append(peer_id)
    if network_id:
        sql += " AND network_id = ?"
        args.append(network_id)
    cur = db.execute(sql, tuple(args))
    return [{'ip': r['ip'], 'port': r['port']} for r in cur.fetchall()]
