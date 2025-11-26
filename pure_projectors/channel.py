"""Pure functional projector for channel events.

Input dict structure:
{
    "event_id": str,
    "event_data": {  # decrypted, verified event payload
        "type": "channel",
        "name": str,
        "group_id": str,
        "signed_by": str,
        "created_at": int,
        "disappearing_time_ms": int,
        "is_main": bool,
        "admin_grant": str | None  # optional admin authorization
    },
    "recorded_by": str,
    "recorded_at": int,
    "dependencies": {
        "signer_user": {  # user who signed this event (via linked_peers lookup)
            "user_id": str,
            "peer_id": str  # = event_data["signed_by"]
        } | None,
        "admin_grant": {  # if event has admin_grant, the admin event
            "event_id": str,
            "user_id": str  # the user this admin grant is for
        } | None
    }
}

Output:
{
    "channels": [row]
}

Note: channel uses INSERT OR REPLACE because user.project() may create
stub channels that need to be overwritten with full data.
"""

from pure_projectors.framework import ProjectorResult


# NOT using @projector decorator because channel needs REPLACE, not IGNORE
# We'll handle this specially in the apply phase

def project(input_dict: dict) -> ProjectorResult:
    """Pure functional projector for channel events.

    Takes a dict with the event and its resolved dependencies.
    Returns a dict of table rows to insert.
    """
    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]

    # Validate required fields
    required = ["name", "group_id", "signed_by", "created_at"]
    for field in required:
        if not event_data.get(field):
            return ProjectorResult(
                valid=False,
                reason=f"Missing required field: {field}"
            )

    # Admin authorization check
    admin_grant_id = event_data.get("admin_grant")
    if admin_grant_id:
        # Need to verify signer is authorized by admin_grant
        signer_user = deps.get("signer_user")
        if not signer_user:
            return ProjectorResult(
                blocked=True,
                missing_deps=["signer_user"]
            )

        admin_grant = deps.get("admin_grant")
        if not admin_grant:
            return ProjectorResult(
                blocked=True,
                missing_deps=["admin_grant"]
            )

        # Verify the admin_grant is for the signer's user
        if admin_grant["user_id"] != signer_user["user_id"]:
            return ProjectorResult(
                valid=False,
                reason=f"admin_grant {admin_grant_id} does not authorize signer user {signer_user['user_id']}"
            )

    # Build channel row
    channel_row = {
        "channel_id": event_id,
        "name": event_data["name"],
        "group_id": event_data["group_id"],
        "signed_by": event_data["signed_by"],
        "created_at": event_data["created_at"],
        "disappearing_time_ms": event_data.get("disappearing_time_ms", 0),
        "is_main": 1 if event_data.get("is_main") else 0,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at
    }

    return ProjectorResult(
        valid=True,
        tables={
            "channels": [channel_row]
        }
    )


# Special metadata for the framework
PROJECT_MODE = "replace"  # Use INSERT OR REPLACE instead of INSERT OR IGNORE
TABLES = ["channels"]
