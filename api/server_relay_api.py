"""Public API endpoints for relay servers.

Servers expose these endpoints for clients to submit invites and sync.
This is a placeholder for the actual FastAPI implementation.

Endpoints:
- POST /api/v1/join - Client submits invite link, server joins network as relay
- WebSocket /sync - Persistent sync connection using existing negentropy protocol
"""

from typing import Any
import logging

log = logging.getLogger(__name__)


# POST /api/v1/join
# Client submits invite link, server joins network as relay
#
# Request:
#   { "invite_link": "quiet://server/..." }
#
# Response (success):
#   {
#     "status": "joined",
#     "peer_shared_id": "abc123...",
#     "sync_endpoint": "wss://relay.example.com:5000/sync"
#   }
#
# Response (error):
#   { "status": "error", "message": "Invalid invite" }


async def handle_join(request: dict, db: Any, t_ms: int, server_address: str) -> dict:
    """Handle POST /api/v1/join - accept invite and join as relay.

    Args:
        request: Request body with invite_link
        db: Database connection
        t_ms: Current timestamp
        server_address: This server's public address (for sync_endpoint)

    Returns:
        Response dict with status, peer_shared_id, sync_endpoint
    """
    from events.identity import server_relay, peer

    invite_link = request.get('invite_link')
    if not invite_link:
        return {'status': 'error', 'message': 'Missing invite_link'}

    if not invite_link.startswith('quiet://server/'):
        return {'status': 'error', 'message': 'Invalid invite link format - must be a server invite'}

    try:
        # Get or create local peer for this server
        peer_id = get_or_create_server_peer(db, t_ms)

        # Join as server relay (not user)
        result = server_relay.join(
            peer_id=peer_id,
            invite_link=invite_link,
            t_ms=t_ms,
            db=db,
        )

        return {
            'status': 'joined',
            'peer_shared_id': result['peer_shared_id'],
            'network_id': result['network_id'],
            'sync_endpoint': f'wss://{server_address}/sync',
        }
    except Exception as e:
        log.error(f"handle_join() failed: {e}")
        return {'status': 'error', 'message': str(e)}


def get_or_create_server_peer(db: Any, t_ms: int) -> str:
    """Get or create a local peer for the server.

    Each server has a single local peer identity that it uses
    to join multiple networks as a relay.

    Args:
        db: Database connection
        t_ms: Current timestamp

    Returns:
        peer_id: The server's local peer ID
    """
    from events.identity import peer
    from core.db import create_unsafe_db

    unsafedb = create_unsafe_db(db)

    # Check if we already have a local peer
    existing = unsafedb.query_one("SELECT peer_id FROM local_peers LIMIT 1")
    if existing:
        return existing['peer_id']

    # Create a new peer for the server
    peer_id = peer.create(t_ms, db)
    log.info(f"get_or_create_server_peer() created new server peer: {peer_id[:20]}...")

    return peer_id


# WebSocket /sync
# Persistent sync connection using existing negentropy protocol
#
# Client connects, provides peer_shared_id
# Server syncs events bidirectionally
# Used for both admin bootstrap and member sync


async def handle_sync_websocket(websocket: Any, db: Any, t_ms: int) -> None:
    """Handle WebSocket /sync - bidirectional event sync.

    This is a placeholder for the actual WebSocket implementation.
    The sync protocol uses the existing negentropy module.

    Args:
        websocket: WebSocket connection
        db: Database connection
        t_ms: Current timestamp
    """
    from events.network import negentropy

    # Placeholder - actual implementation would:
    # 1. Receive peer_shared_id from client
    # 2. Establish connection in connections table
    # 3. Run negentropy sync protocol over WebSocket
    # 4. Handle incoming/outgoing events

    log.info("handle_sync_websocket() - placeholder implementation")
    pass


# FastAPI route registration (placeholder)
#
# In a real implementation, this would be:
#
# from fastapi import FastAPI, WebSocket
# from fastapi.responses import JSONResponse
#
# app = FastAPI()
#
# @app.post("/api/v1/join")
# async def join_endpoint(request: JoinRequest):
#     return await handle_join(request.dict(), db, get_time_ms(), SERVER_ADDRESS)
#
# @app.websocket("/sync")
# async def sync_endpoint(websocket: WebSocket):
#     await websocket.accept()
#     await handle_sync_websocket(websocket, db, get_time_ms())
