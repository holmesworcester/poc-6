"""
Queries for key event type.
"""
from core.db import ReadOnlyConnection
from typing import Dict, Any, List, TypedDict
import sqlite3
from core.queries import query


class KeyRecord(TypedDict, total=False):
    key_id: str
    group_id: str
    peer_id: str
    created_at: int


class KeyListParams(TypedDict, total=False):
    group_id: str


@query(param_type=KeyListParams, result_type=list[KeyRecord])
def list(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List all keys, optionally filtered by group_id."""
    group_id = params.get('group_id')
    
    if group_id:
        cursor = db.execute("""
            SELECT key_id, group_id, peer_id, created_at 
            FROM group_keys 
            WHERE group_id = ?
            ORDER BY created_at DESC
        """, (group_id,))
    else:
        cursor = db.execute("""
            SELECT key_id, group_id, peer_id, created_at 
            FROM group_keys 
            ORDER BY created_at DESC
        """)
    
    results = []
    for row in cursor:
        results.append({
            "key_id": row['key_id'],
            "group_id": row['group_id'],
            "peer_id": row['peer_id'],
            "created_at": row['created_at']
        })
    
    return results
