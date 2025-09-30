"""
Queries for group event type.
"""
from core.db import ReadOnlyConnection
from typing import Dict, Any, List, TypedDict
import sqlite3
from core.queries import query
from core.seen import has_seen


class GroupRecord(TypedDict, total=False):
    group_id: str
    name: str
    creator_id: str
    network_id: str
    created_at: int
    member_count: int


class GroupGetParams(TypedDict, total=False):
    peer_secret_id: str
    network_id: str
    user_id: str
import json


@query(param_type=GroupGetParams, result_type=list[GroupRecord])
def get(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    List groups visible to a local peer (peer_secret).

    Required params:
    - peer_secret_id: Local peer requesting the groups

    Optional params:
    - network_id: Filter by network
    - user_id: Filter by groups the user is member of
    """
    # identity_id is required for access control
    peer_secret_id = params.get('peer_secret_id')
    if not peer_secret_id:
        raise ValueError("peer_secret_id is required for group.get")

    network_id = params.get('network_id')
    owner_id = params.get('owner_id')
    user_id = params.get('user_id')

    # Only show groups the identity has access to
    # TODO: Check group membership properly
    base_select = "SELECT g.*, COALESCE((SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.group_id), 0) AS member_count FROM groups g"
    query = f"""
        {base_select}
        WHERE EXISTS (
            SELECT 1 FROM users u_me 
            JOIN peers p_me ON p_me.peer_id = u_me.peer_id
            WHERE p_me.peer_secret_id = ?
              AND u_me.network_id = g.network_id
        )
    """
    query_params = [peer_secret_id]
    
    if network_id:
        query += " AND g.network_id = ?"
        query_params.append(network_id)
    if owner_id:
        query += " AND g.owner_id = ?"
        query_params.append(owner_id)
    
    if user_id:
        # Filter by groups the user is a member of
        query = f"""
        SELECT g.*, COALESCE((SELECT COUNT(*) FROM group_members gm2 WHERE gm2.group_id = g.group_id), 0) AS member_count FROM groups g
        JOIN group_members gm ON g.group_id = gm.group_id
        WHERE gm.user_id = ?
        AND EXISTS (
            SELECT 1 FROM users u_me JOIN peers p_me ON p_me.peer_id = u_me.peer_id
            WHERE p_me.peer_secret_id = ?
              AND u_me.network_id = g.network_id
        )
        """
        query_params = [user_id, peer_secret_id]
        if network_id:
            query += " AND g.network_id = ?"
            query_params.append(network_id)
        if owner_id:
            query += " AND g.owner_id = ?"
            query_params.append(owner_id)
    
    query += " ORDER BY created_at DESC"
    
    cursor = db.execute(query, tuple(query_params))
    groups: list[dict[str, Any]] = []
    for row in cursor:
        group = dict(row)
        # Parse permissions JSON
        if group.get('permissions'):
            group['permissions'] = json.loads(group['permissions'])
        if has_seen(db, peer_secret_id, group['group_id']):
            groups.append(group)
    
    return groups
