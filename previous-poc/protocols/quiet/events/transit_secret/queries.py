"""
Transit secret queries for bootstrap selection.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from core.queries import query
from core.db import ReadOnlyConnection


@query
def get_bootstrap_for_network(db: ReadOnlyConnection, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return latest transit bootstrap info for a network.

    Returns: {transit_secret_id, dest_ip, dest_port}
    """
    nid = params.get('network_id')
    if not isinstance(nid, str) or not nid:
        return None
    row = db.execute(
        """
        SELECT transit_secret_id, dest_ip, dest_port
        FROM transit_bootstrap
        WHERE network_id = ?
        LIMIT 1
        """,
        (nid,),
    ).fetchone()
    if not row:
        return None
    return {
        'transit_secret_id': row['transit_secret_id'],
        'dest_ip': row['dest_ip'],
        'dest_port': row['dest_port'],
    }

