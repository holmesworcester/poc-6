"""Network routes.

Endpoints:
    GET  /networks                 - List networks this peer belongs to
    GET  /networks/{network_id}    - Get network details
    POST /networks                 - Create new network
    POST /networks/join            - Join network via invite link
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.core.database import get_db, get_peer_id, get_safe_db, get_t_ms
from events.identity import network, user

router = APIRouter()


class NetworkResponse(BaseModel):
    network_id: str
    name: str | None = None
    created_at: int | None = None


class NetworkListResponse(BaseModel):
    items: list[NetworkResponse]


class CreateNetworkRequest(BaseModel):
    name: str
    username: str = "User"
    device_name: str = "Device"


class CreateNetworkResponse(BaseModel):
    network_id: str
    user_id: str
    peer_shared_id: str
    channel_id: str


class JoinNetworkRequest(BaseModel):
    invite_link: str
    username: str
    device_name: str = "Device"


class JoinNetworkResponse(BaseModel):
    network_id: str
    user_id: str
    peer_shared_id: str


@router.get("/networks", response_model=NetworkListResponse)
async def list_networks():
    """List networks this peer belongs to."""
    peer_id = get_peer_id()
    db = get_db()

    # Get the network for this peer (simpler query by recorded_by)
    network_id = network.get_network_id_for_peer(peer_id, db)

    if network_id:
        # Get full network details
        from core.db import create_safe_db
        safedb = create_safe_db(db, recorded_by=peer_id)
        network_row = safedb.query_one(
            "SELECT network_id, created_at FROM networks WHERE network_id = ? AND recorded_by = ?",
            (network_id, peer_id)
        )
        if network_row:
            return NetworkListResponse(
                items=[
                    NetworkResponse(
                        network_id=network_row["network_id"],
                        name=None,  # Network name stored separately
                        created_at=network_row.get("created_at"),
                    )
                ]
            )
    return NetworkListResponse(items=[])


@router.get("/networks/{network_id}", response_model=NetworkResponse)
async def get_network(network_id: str):
    """Get network details."""
    peer_id = get_peer_id()
    db = get_db()

    # Verify this peer has access to this network
    peer_network_id = network.get_network_id_for_peer(peer_id, db)
    if not peer_network_id or peer_network_id != network_id:
        raise HTTPException(status_code=404, detail="Network not found")

    # Get network details
    from core.db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)
    network_row = safedb.query_one(
        "SELECT network_id, created_at FROM networks WHERE network_id = ? AND recorded_by = ?",
        (network_id, peer_id)
    )

    if not network_row:
        raise HTTPException(status_code=404, detail="Network not found")

    return NetworkResponse(
        network_id=network_row["network_id"],
        name=None,
        created_at=network_row.get("created_at"),
    )


@router.post("/networks", response_model=CreateNetworkResponse, status_code=201)
async def create_network(request: CreateNetworkRequest):
    """Create a new network."""
    db = get_db()
    t_ms = get_t_ms()

    result = user.new_network(
        name=request.username,
        t_ms=t_ms,
        db=db,
        device_name=request.device_name,
        network_name=request.name,
    )
    db.commit()

    return CreateNetworkResponse(
        network_id=result["network_id"],
        user_id=result["user_id"],
        peer_shared_id=result["peer_shared_id"],
        channel_id=result["channel_id"],
    )


@router.post("/networks/join", response_model=JoinNetworkResponse, status_code=201)
async def join_network(request: JoinNetworkRequest):
    """Join a network via invite link."""
    db = get_db()
    t_ms = get_t_ms()
    peer_id = get_peer_id()

    try:
        result = user.join(
            peer_id=peer_id,
            invite_link=request.invite_link,
            name=request.username,
            t_ms=t_ms,
            db=db,
            device_name=request.device_name,
        )
        db.commit()

        return JoinNetworkResponse(
            network_id=result["network_id"],
            user_id=result["user_id"],
            peer_shared_id=result["peer_shared_id"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
