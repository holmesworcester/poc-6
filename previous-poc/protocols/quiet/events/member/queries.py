"""
Queries for members.
"""
from __future__ import annotations

from typing import Any, Dict, List
from core.db import ReadOnlyConnection
from core.queries import query


@query
def list_by_group(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List members (users) of a group."""
    group_id = params.get('group_id')
    if not isinstance(group_id, str) or not group_id:
        return []
    cur = db.execute(
        """
        SELECT u.user_id, u.name, u.peer_id, u.created_at
        FROM users u
        JOIN group_members gm ON u.user_id = gm.user_id
        WHERE gm.group_id = ?
        ORDER BY u.created_at DESC
        """,
        (group_id,),
    )
    rows = cur.fetchall()
    members: list[Dict[str, Any]] = []
    for r in rows:
        members.append({
            'user_id': r['user_id'],
            'name': r['name'],
            'peer_id': r['peer_id'],
            'created_at': r['created_at'],
        })
    return members

