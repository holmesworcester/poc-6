"""Sync status routes.

Endpoints:
    GET /networks/{network_id}/sync/status - Get peer connection and sync status
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.core.database import get_db, get_peer_id, get_t_ms
from events.identity import network
from events.network import connection_request as conn_module
from core.db import create_safe_db

router = APIRouter()


class PeerStatus(BaseModel):
    peer_shared_id: str
    connected: bool
    is_active: bool
    is_pending: bool
    last_handshake_ms: int | None = None


class SyncStatusResponse(BaseModel):
    network_id: str
    connected_peers: int
    total_peers: int
    active_connections: int
    pending_connections: int
    peers: list[PeerStatus]
    pending_events: int


@router.get(
    "/networks/{network_id}/sync/status", response_model=SyncStatusResponse
)
async def get_sync_status(network_id: str):
    """Get network sync and peer connection status."""
    peer_id = get_peer_id()
    db = get_db()
    t_ms = get_t_ms()

    # Verify peer is in this network
    network_info = network.get_for_peer(peer_id, peer_id, db)
    if not network_info or network_info["network_id"] != network_id:
        raise HTTPException(status_code=404, detail="Network not found")

    # Get connection statuses using connection module
    conn_display = conn_module.list_all_for_display(peer_id, t_ms, db)

    # Build peer status list from all connections
    peer_statuses = []
    seen_peers = set()

    # Active connections (bidirectional)
    for conn in conn_display.get("active", []):
        if conn.peer_shared_id and conn.peer_shared_id not in seen_peers:
            seen_peers.add(conn.peer_shared_id)
            peer_statuses.append(
                PeerStatus(
                    peer_shared_id=conn.peer_shared_id,
                    connected=True,
                    is_active=True,
                    is_pending=False,
                    last_handshake_ms=conn.last_handshake_ms,
                )
            )

    # Pending connections (awaiting ack)
    for conn in conn_display.get("pending", []):
        if conn.peer_shared_id and conn.peer_shared_id not in seen_peers:
            seen_peers.add(conn.peer_shared_id)
            peer_statuses.append(
                PeerStatus(
                    peer_shared_id=conn.peer_shared_id,
                    connected=False,
                    is_active=False,
                    is_pending=True,
                    last_handshake_ms=conn.last_handshake_ms,
                )
            )

    # Count connected vs total
    connected_count = len(conn_display.get("active", []))
    total_peers = len(peer_statuses)

    # Count pending events in incoming queue
    safedb = create_safe_db(db, recorded_by=peer_id)
    from core.db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    pending_row = unsafedb.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")
    pending_events = pending_row["cnt"] if pending_row else 0

    return SyncStatusResponse(
        network_id=network_id,
        connected_peers=connected_count,
        total_peers=total_peers,
        active_connections=len(conn_display.get("active", [])),
        pending_connections=len(conn_display.get("pending", [])),
        peers=peer_statuses,
        pending_events=pending_events,
    )
