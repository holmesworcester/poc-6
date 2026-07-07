"""Transport control routes (QUIC relay)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.core.database import get_db, get_peer_id
from events.identity import peer_shared

router = APIRouter()


class QuicConnectRequest(BaseModel):
    relay_url: str
    insecure: bool = False


class QuicStatusResponse(BaseModel):
    active: bool
    relay_url: str | None = None


@router.post("/transport/quic", response_model=QuicStatusResponse)
async def connect_quic(req: QuicConnectRequest):
    """Connect to a QUIC relay for this peer."""
    from core import transport

    peer_id = get_peer_id()
    db = get_db()
    ps = peer_shared.get_for_peer(peer_id, peer_id, db)
    if not ps or not ps.get("peer_shared_id"):
        raise HTTPException(status_code=400, detail="peer_shared_id not available")

    try:
        transport.start_quic_relay(req.relay_url, ps["peer_shared_id"], insecure=req.insecure)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return QuicStatusResponse(active=True, relay_url=req.relay_url)


@router.delete("/transport/quic", response_model=QuicStatusResponse)
async def disconnect_quic():
    """Disconnect from the QUIC relay."""
    from core import transport
    transport.stop_quic_relay()
    return QuicStatusResponse(active=False)
