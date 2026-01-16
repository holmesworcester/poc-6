"""Channel routes.

Endpoints:
    GET  /networks/{network_id}/channels              - List channels
    GET  /networks/{network_id}/channels/{channel_id} - Get channel details
    POST /networks/{network_id}/channels              - Create channel (admin only)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.core.database import get_db, get_peer_id, get_t_ms, verify_network_access, from_urlsafe_b64
from events.content import channel
from events.identity import network, invite
from events.identity.peer_shared import get_peer_id_for_signing

router = APIRouter()


class ChannelResponse(BaseModel):
    channel_id: str
    name: str
    group_id: str
    disappearing_time_ms: int = 0
    is_main: bool = False
    created_at: int | None = None


class ChannelListResponse(BaseModel):
    items: list[ChannelResponse]


class CreateChannelRequest(BaseModel):
    name: str
    disappearing_time_ms: int = 0


class CreateChannelResponse(BaseModel):
    channel_id: str


@router.get("/networks/{network_id}/channels", response_model=ChannelListResponse)
async def list_channels(network_id: str):
    """List channels in the network."""
    peer_id = verify_network_access(network_id)
    db = get_db()

    channels = channel.list_channels(recorded_by=peer_id, db=db)

    return ChannelListResponse(
        items=[
            ChannelResponse(
                channel_id=ch["channel_id"],
                name=ch["name"],
                group_id=ch["group_id"],
                disappearing_time_ms=ch.get("disappearing_time_ms", 0),
                is_main=ch.get("is_main", False),
                created_at=ch.get("created_at"),
            )
            for ch in channels
        ]
    )


@router.get(
    "/networks/{network_id}/channels/{channel_id}", response_model=ChannelResponse
)
async def get_channel(network_id: str, channel_id: str):
    """Get channel details."""
    peer_id = verify_network_access(network_id)
    db = get_db()

    # Convert from URL-safe base64
    channel_id = from_urlsafe_b64(channel_id)

    ch = channel.get_by_id(channel_id, recorded_by=peer_id, db=db)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    return ChannelResponse(
        channel_id=ch["channel_id"],
        name=ch["name"],
        group_id=ch["group_id"],
        disappearing_time_ms=ch.get("disappearing_time_ms", 0),
        is_main=ch.get("is_main", False),
        created_at=ch.get("created_at"),
    )


@router.post(
    "/networks/{network_id}/channels",
    response_model=CreateChannelResponse,
    status_code=201,
)
async def create_channel(network_id: str, request: CreateChannelRequest):
    """Create a new channel (admin only)."""
    peer_id = verify_network_access(network_id)
    db = get_db()
    t_ms = get_t_ms()

    # Convert from URL-safe base64 for use with event modules
    network_id_std = from_urlsafe_b64(network_id)

    # Get peer_shared_id from peer_id via peer_self table
    from core.db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)
    peer_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_row:
        raise HTTPException(status_code=400, detail="Peer identity not found")
    peer_shared_id = peer_row["peer_shared_id"]

    # Check if user is admin
    if not invite.is_admin(peer_shared_id, peer_id, db):
        raise HTTPException(status_code=403, detail="Only admins can create channels")

    # Get all_users group for the channel
    all_users_group_id = network.get_all_users_group_id(network_id_std, peer_id, db)

    try:
        channel_id = channel.create(
            name=request.name,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            db=db,
            group_id=all_users_group_id,
            disappearing_time_ms=request.disappearing_time_ms,
        )
        db.commit()

        return CreateChannelResponse(channel_id=channel_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
