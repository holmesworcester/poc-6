"""User routes.

Endpoints:
    GET    /networks/{network_id}/users           - List users in network
    GET    /networks/{network_id}/users/{user_id} - Get user details
    DELETE /networks/{network_id}/users/{user_id} - Remove user (admin only)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.core.database import get_db, get_peer_id, get_t_ms, verify_network_access, from_urlsafe_b64
from events.identity import network, invite, user_removed
from events.group import group_member

router = APIRouter()


class UserResponse(BaseModel):
    user_id: str
    name: str | None = None
    created_at: int | None = None


class UserListResponse(BaseModel):
    items: list[UserResponse]


@router.get("/networks/{network_id}/users", response_model=UserListResponse)
async def list_users(network_id: str):
    """List users in the network."""
    peer_id = verify_network_access(network_id)
    db = get_db()

    # Convert from URL-safe base64 for use with event modules
    network_id_std = from_urlsafe_b64(network_id)

    # Get all_users group and list members
    all_users_group_id = network.get_all_users_group_id(network_id_std, peer_id, db)
    members = group_member.list_members(all_users_group_id, peer_id, db)

    return UserListResponse(
        items=[
            UserResponse(
                user_id=member["user_id"],
                name=member.get("name"),
                created_at=member.get("created_at"),
            )
            for member in members
        ]
    )


@router.get("/networks/{network_id}/users/{user_id}", response_model=UserResponse)
async def get_user(network_id: str, user_id: str):
    """Get user details."""
    peer_id = verify_network_access(network_id)
    db = get_db()

    # Convert from URL-safe base64
    user_id = from_urlsafe_b64(user_id)

    # Get all_users group and find the user
    # Note: network_id is already converted by verify_network_access
    network_id_std = from_urlsafe_b64(network_id)
    all_users_group_id = network.get_all_users_group_id(network_id_std, peer_id, db)
    members = group_member.list_members(all_users_group_id, peer_id, db)

    for member in members:
        if member["user_id"] == user_id:
            return UserResponse(
                user_id=member["user_id"],
                name=member.get("name"),
                created_at=member.get("created_at"),
            )

    raise HTTPException(status_code=404, detail="User not found")


@router.delete("/networks/{network_id}/users/{user_id}")
async def remove_user(network_id: str, user_id: str):
    """Remove a user from the network (admin only)."""
    peer_id = verify_network_access(network_id)
    db = get_db()
    t_ms = get_t_ms()

    # Convert from URL-safe base64
    user_id = from_urlsafe_b64(user_id)

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
        raise HTTPException(status_code=403, detail="Only admins can remove users")

    try:
        user_removed.create(
            removed_user_id=user_id,
            removed_by_peer_id=peer_shared_id,
            removed_by_local_peer_id=peer_id,
            t_ms=t_ms,
            db=db,
        )
        db.commit()
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
