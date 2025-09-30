"""
Queries for message event type.
"""
from core.db import ReadOnlyConnection
from typing import Dict, Any, List, TypedDict
import sqlite3
from core.queries import query
from core.seen import has_seen


class MessageRecord(TypedDict, total=False):
    message_id: str
    content: str
    channel_id: str
    author_id: str
    created_at: int
    author_name: str


class MessageGetParams(TypedDict, total=False):
    peer_secret_id: str
    channel_id: str
    group_id: str
    limit: int
    offset: int


@query(param_type=MessageGetParams, result_type=list[MessageRecord])
def get(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    List messages visible to a local peer (peer_secret).

    Required params:
    - peer_secret_id: Local peer requesting the messages

    Optional params:
    - channel_id: Filter by channel (recommended)
    - group_id: Filter by group
    - limit: Maximum number of messages (default 100)
    - offset: Offset for pagination
    """
    # identity_id is required for access control
    peer_secret_id = params.get('peer_secret_id')
    if not peer_secret_id:
        raise ValueError("peer_secret_id is required for message.get")

    channel_id = params.get('channel_id')
    group_id = params.get('group_id')
    limit = params.get('limit', 100)
    offset = params.get('offset', 0)
    
    # Check that identity exists
    # TODO: Add proper group membership check using members table
    # Should verify: identity -> user -> member -> group -> channel
    # For now, just verify the identity exists and allow access if channel_id is provided
    # Enforce access: identity must have SEEN the event AND be a user in the message's network.
    # Seen gating is enforced in Python for O(1) checks against ES (events table).
    # Membership link: identities(identity_id) -> peers(peer_id, identity_id) -> users(peer_id, network_id)
    # Only display messages whose authors are known (joined users row must exist).
    query = """
        SELECT 
            m.*, 
            u_author.name AS author_name
        FROM messages m
        JOIN users u_author ON u_author.peer_id = m.author_id
        WHERE EXISTS (
            SELECT 1 
            FROM users u_me 
            JOIN peers p_me ON p_me.peer_id = u_me.peer_id
            WHERE p_me.peer_secret_id = ?
              AND u_me.network_id = m.network_id
        )
    """
    query_params = [peer_secret_id]

    if channel_id:
        query += " AND m.channel_id = ?"
        query_params.append(channel_id)

    if group_id:
        query += " AND m.group_id = ?"
        query_params.append(group_id)
    
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    query_params.extend([limit, offset])
    
    cursor = db.execute(query, tuple(query_params))
    # O(1) seen gating per row
    messages: list[dict[str, Any]] = []
    for row in cursor:
        row_d = dict(row)
        if has_seen(db, peer_secret_id, row_d['message_id']):
            messages.append(row_d)
    
    # Return in chronological order for display
    messages.reverse()
    
    return messages
