"""
Network queries.
"""
from typing import Dict, Any, List, TypedDict
from core.queries import query
from core.db import ReadOnlyConnection
from core.seen import has_seen


class NetworkRecord(TypedDict, total=False):
    network_id: str
    name: str
    creator_id: str
    created_at: int


class NetworkGetParams(TypedDict, total=False):
    peer_secret_id: str
    network_id: str


@query(param_type=NetworkGetParams, result_type=list[NetworkRecord])
def get(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Get networks visible to a local peer (peer_secret), with optional filtering."""
    peer_secret_id = params.get('peer_secret_id')
    if not peer_secret_id:
        raise ValueError("peer_secret_id is required")

    query_str = (
        "SELECT * FROM networks n "
        "WHERE EXISTS ("
        " SELECT 1 FROM users u_me JOIN peers p_me ON p_me.peer_id = u_me.peer_id"
        " WHERE p_me.peer_secret_id = ? AND u_me.network_id = n.network_id)"
    )
    query_params: List[Any] = [peer_secret_id]

    # Filter by network_id if provided
    if params.get('network_id'):
        query_str += " AND network_id = ?"
        query_params.append(params['network_id'])

    cursor = db.execute(query_str, tuple(query_params))
    columns = [desc[0] for desc in cursor.description]
    results: list[dict[str, Any]] = []
    
    for row in cursor.fetchall():
        network = dict(zip(columns, row))
        if has_seen(db, peer_secret_id, network['network_id']):
            results.append(network)
    
    return results
