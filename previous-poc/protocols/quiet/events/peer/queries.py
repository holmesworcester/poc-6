"""
Peer queries (read-only) for flows and jobs.
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict
from core.queries import query
from core.db import ReadOnlyConnection


class PeerRecord(TypedDict, total=False):
    peer_id: str
    peer_secret_id: str
    public_key: str
    created_at: int


@query(result_type=list[PeerRecord])
def list_local(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List peers that are locally owned (i.e., have a local peer_secret).

    Ownership is determined by joining `peers.peer_secret_id` to
    `peer_secrets.peer_secret_id`.
    """
    cur = db.execute(
        """
        SELECT p.peer_id, p.peer_secret_id, p.public_key, p.created_at
        FROM peers p
        JOIN peer_secrets s ON s.peer_secret_id = p.peer_secret_id
        ORDER BY p.created_at DESC
        """
    )
    return [dict(r) for r in cur.fetchall()]

