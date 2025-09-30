"""
Queries for channel event type.
"""
from core.db import ReadOnlyConnection
from typing import Dict, Any, List, TypedDict, Optional
import sqlite3
from core.queries import query
from core.seen import has_seen


class ChannelRecord(TypedDict, total=False):
    channel_id: str
    name: str
    group_id: str
    network_id: str
    created_at: int


class ChannelGetParams(TypedDict, total=False):
    peer_secret_id: str
    group_id: str
    network_id: str


class ChannelGetByIdParams(TypedDict):
    channel_id: str


@query(param_type=ChannelGetParams, result_type=list[ChannelRecord])
def get(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    List channels visible to a local peer (peer_secret).

    Required params:
    - peer_secret_id: Local peer requesting the channels

    Optional params:
    - group_id: Filter by group
    - network_id: Filter by network
    """
    # identity_id is required for access control
    peer_secret_id = params.get('peer_secret_id')
    if not peer_secret_id:
        raise ValueError("peer_secret_id is required for channel.get")

    group_id = params.get('group_id')
    network_id = params.get('network_id')

    # Base selection; seen gating is enforced in Python using ES O(1) check
    query = """
        SELECT c.* FROM channels c
        WHERE EXISTS (
            SELECT 1 
            FROM users u_me
            JOIN peers p_me ON p_me.peer_id = u_me.peer_id
            WHERE p_me.peer_secret_id = ?
              AND u_me.network_id = c.network_id
        )
    """
    query_params = [peer_secret_id]
    
    if group_id:
        query += " AND group_id = ?"
        query_params.append(group_id)
    
    if network_id:
        query += " AND network_id = ?"
        query_params.append(network_id)
    
    query += " ORDER BY created_at DESC"
    
    cursor = db.execute(query, tuple(query_params))
    channels: list[dict[str, Any]] = []
    for row in cursor:
        row_d = dict(row)
        if has_seen(db, peer_secret_id, row_d['channel_id']):
            channels.append(row_d)

    return channels


@query(param_type=ChannelGetByIdParams, result_type=Optional[ChannelRecord])
def get_by_id(db: ReadOnlyConnection, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get a specific channel by ID.

    This query doesn't require peer_secret_id since it's used internally
    by flows that already have access to the channel_id.

    Required params:
    - channel_id: The channel to retrieve
    """
    channel_id = params.get('channel_id')
    if not channel_id:
        raise ValueError("channel_id is required for channel.get_by_id")

    # First try channels table (if channel has been projected)
    cursor = db.execute("""
        SELECT channel_id, name, group_id, network_id, creator_id, created_at
        FROM channels
        WHERE channel_id = ?
    """, (channel_id,))
    row = cursor.fetchone()

    if row:
        return dict(row)

    # Fall back to events table if not in channels table yet
    cursor = db.execute("""
        SELECT
            event_id as channel_id,
            json_extract(event_plaintext, '$.name') as name,
            json_extract(event_plaintext, '$.group_id') as group_id,
            json_extract(event_plaintext, '$.network_id') as network_id,
            json_extract(event_plaintext, '$.creator_id') as creator_id,
            json_extract(event_plaintext, '$.created_at') as created_at
        FROM events
        WHERE event_id = ? AND event_type = 'channel'
    """, (channel_id,))
    row = cursor.fetchone()

    if row:
        return dict(row)

    return None
