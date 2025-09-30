"""
Queries for peer_secret (local identities).

These are projection-backed convenience queries for UI account selection.
Business logic should resolve local-only peer_secret via ES when possible.
"""
from typing import Dict, Any, List, TypedDict, Optional
from core.db import ReadOnlyConnection
from core.queries import query


class PeerSecretRecord(TypedDict, total=False):
    peer_secret_id: str
    name: str
    public_key: str
    created_at: int


@query(result_type=list[PeerSecretRecord])
def list(db: ReadOnlyConnection, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List all local peer_secret accounts from the projection table."""
    cur = db.execute(
        """
        SELECT peer_secret_id, name, public_key, created_at
        FROM peer_secrets
        ORDER BY created_at DESC
        """
    )
    return [dict(r) for r in cur.fetchall()]


@query(result_type=Optional[PeerSecretRecord])
def get(db: ReadOnlyConnection, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Get a specific local peer_secret by id."""
    pid = params.get('peer_secret_id')
    if not pid:
        raise ValueError('peer_secret_id is required')
    cur = db.execute(
        """
        SELECT peer_secret_id, name, public_key, created_at
        FROM peer_secrets
        WHERE peer_secret_id = ?
        LIMIT 1
        """,
        (pid,),
    )
    row = cur.fetchone()
    return dict(row) if row else None

