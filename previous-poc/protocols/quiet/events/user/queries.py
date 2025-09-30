"""
Queries for user event type.
"""
from core.db import ReadOnlyConnection
from typing import Dict, Any, List, Optional, TypedDict
import sqlite3
from core.queries import query
from core.seen import has_seen


class UserRecord(TypedDict, total=False):
    user_id: str
    peer_id: str
    network_id: str
    name: str
    joined_at: int


class UserGetParams(TypedDict):
    peer_secret_id: str
    network_id: str
    limit: int
    offset: int


@query(param_type=UserGetParams, result_type=list[UserRecord])
def get(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    List users visible to a local peer (peer_secret) in a network.

    Required params:
    - peer_secret_id: Local peer requesting the users
    - network_id: The network to list users for

    Optional params:
    - limit: Maximum number of users to return (default 100)
    - offset: Offset for pagination
    """
    peer_secret_id = params.get('peer_secret_id')
    if not peer_secret_id:
        raise ValueError("peer_secret_id is required for user.get")

    network_id = params.get('network_id')
    if not network_id:
        raise ValueError("network_id is required")
    
    limit = params.get('limit', 100)
    offset = params.get('offset', 0)
    
    # Only show users if the identity has access to the network
    # TODO: Properly check network membership
    cursor = db.execute(
        """
        SELECT u.*
        FROM users u
        WHERE u.network_id = ?
          AND EXISTS (
            SELECT 1 FROM users u_me
            JOIN peers p_me ON p_me.peer_id = u_me.peer_id
            WHERE p_me.peer_secret_id = ?
              AND u_me.network_id = u.network_id
          )
        ORDER BY u.joined_at DESC
        LIMIT ? OFFSET ?
        """,
        (network_id, peer_secret_id, limit, offset)
    )
    
    users: list[dict[str, Any]] = []
    for row in cursor:
        row_d = dict(row)
        if has_seen(db, peer_secret_id, row_d['user_id']):
            users.append(row_d)
    
    return users


@query
def get_user(db: ReadOnlyConnection, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get a specific user by ID.
    
    Required params:
    - user_id: The user to look up
    """
    user_id = params.get('user_id')
    
    if not user_id:
        raise ValueError("user_id is required")
    
    cursor = db.execute(
        """
        SELECT u.*
        FROM users u
        WHERE u.user_id = ?
        """,
        (user_id,)
    )
    
    row = cursor.fetchone()
    if not row:
        return None
    identity_id = params.get('peer_secret_id') or params.get('identity_id')
    rd = dict(row)
    if identity_id and has_seen(db, identity_id, rd['user_id']):
        return rd
    return None


@query
def get_user_by_peer_id(db: ReadOnlyConnection, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get a user by their peer ID in a specific network.
    
    Required params:
    - peer_id: The peer ID to look up
    - network_id: The network to search in
    """
    peer_id = params.get('peer_id')
    network_id = params.get('network_id')
    
    if not peer_id or not network_id:
        raise ValueError("peer_id and network_id are required")
    
    cursor = db.execute(
        """
        SELECT u.*
        FROM users u
        WHERE u.peer_id = ? AND u.network_id = ?
        ORDER BY u.joined_at DESC
        LIMIT 1
        """,
        (peer_id, network_id)
    )
    
    row = cursor.fetchone()
    if not row:
        return None
    identity_id = params.get('peer_secret_id') or params.get('identity_id')
    rd = dict(row)
    if identity_id and has_seen(db, identity_id, rd['user_id']):
        return rd
    return None


@query
def count_users(db: ReadOnlyConnection, params: Dict[str, Any]) -> int:
    """
    Count users in a network.
    
    Required params:
    - network_id: The network to count users for
    """
    network_id = params.get('network_id')
    if not network_id:
        raise ValueError("network_id is required")
    
    cursor = db.execute(
        """
        SELECT COUNT(*) as count
        FROM users
        WHERE network_id = ?
        """,
        (network_id,)
    )
    
    row = cursor.fetchone()
    return row['count'] if row else 0


@query
def is_user_in_network(db: ReadOnlyConnection, params: Dict[str, Any]) -> bool:
    """
    Check if a peer has joined a network as a user.
    
    Required params:
    - peer_id: The peer to check
    - network_id: The network to check
    """
    peer_id = params.get('peer_id')
    network_id = params.get('network_id')
    
    if not peer_id or not network_id:
        raise ValueError("peer_id and network_id are required")
    
    cursor = db.execute(
        """
        SELECT 1 FROM users 
        WHERE peer_id = ? AND network_id = ?
        """,
        (peer_id, network_id)
    )
    
    return cursor.fetchone() is not None


@query
def list_by_network(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List peers by network.

    Params:
    - network_id (optional): If provided, returns [{'peer_id','network_id'}] for that network.
      If omitted, returns distinct pairs across all networks with network_id != ''.
    """
    network_id = params.get('network_id')
    rows: List[Dict[str, Any]] = []
    if isinstance(network_id, str) and network_id:
        cur = db.execute(
            """
            SELECT DISTINCT u.peer_id AS peer_id
            FROM users u
            WHERE u.network_id = ?
            """,
            (network_id,),
        )
        rows = [{'peer_id': r['peer_id'], 'network_id': network_id} for r in cur.fetchall()]
    else:
        cur = db.execute(
            """
            SELECT DISTINCT u.peer_id AS peer_id, u.network_id AS network_id
            FROM users u
            WHERE u.network_id != ''
            """,
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows


class UserListForPeerParams(TypedDict, total=False):
    peer_id: str
    network_id: str


@query(param_type=UserListForPeerParams, result_type=list[UserRecord])
def list_for_peer(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List user rows for a given peer (optionally within a network).

    - peer_id: required
    - network_id: optional; if provided, filters by network and ensures uniqueness.

    In normal operation there should be exactly one user per (peer_id, network_id).
    If more than one row is found for the given filters, raises ValueError.
    """
    peer_id = params.get('peer_id')
    if not isinstance(peer_id, str) or not peer_id:
        raise ValueError('peer_id is required')

    network_id = params.get('network_id')
    if isinstance(network_id, str) and network_id:
        cur = db.execute(
            """
            SELECT u.*
            FROM users u
            WHERE u.peer_id = ? AND u.network_id = ?
            ORDER BY u.joined_at DESC
            """,
            (peer_id, network_id),
        )
    else:
        cur = db.execute(
            """
            SELECT u.*
            FROM users u
            WHERE u.peer_id = ?
            ORDER BY u.joined_at DESC
            """,
            (peer_id,),
        )

    rows = [dict(r) for r in cur.fetchall()]
    if len(rows) > 1:
        raise ValueError(f"Expected at most one user for peer {peer_id} in network {network_id or '*'}, found {len(rows)}")
    return rows
