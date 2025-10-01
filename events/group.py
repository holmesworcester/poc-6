"""Group management functions."""
from typing import Any

def create(params: dict[str, Any], t_ms: int, db: Any) -> dict[str, Any]:
    """Create a new group."""
    created_by = params['created_by']
    all_groups = list_all_groups(created_by, db)
    # TODO: implement group creation logic (depends on signing / sig verification)
    return {"id": "new_group_id", "groups": all_groups}


def list_all_groups(seen_by_peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all groups for a specific peer."""
    # TODO: implement - should filter by seen_by_peer_id
    return []


def pick_key(group_id: str, db: Any) -> Any:
    """Pick an appropriate key for a group."""
    # Query the group table by group_id and return the latest key for this group
    result = db.query_one("SELECT key FROM groups WHERE id = ? ORDER BY created_at DESC LIMIT 1", (group_id,))
    return result['key'] if result else None


def project(event_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> str | None:
    """Project a single group event into the database."""
    # TODO: implement
    return None