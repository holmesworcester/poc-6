"""Pure functional projector for group_member events.

Input dict structure:
{
    "event_id": str,
    "event_data": {  # decrypted, verified event payload
        "type": "group_member",
        "group_id": str,
        "user_id": str,  # user being added
        "added_by": str,  # peer_shared_id who added them
        "created_at": int,
        "admin_grant": str | None  # optional admin authorization
    },
    "recorded_by": str,
    "recorded_at": int,
    "dependencies": {
        "group": {  # the group this member is being added to
            "event_id": str,
            "event_data": {
                "name": str,
                "signed_by": str,  # group creator
                ...
            }
        },
        "user": {  # the user being added
            "event_id": str,  # = event_data["user_id"]
            "event_data": {
                "name": str,
                ...
            }
        },
        "adder_user": {  # user who added this member (via linked_peers lookup)
            "user_id": str,
            "peer_id": str  # = event_data["added_by"]
        } | None,
        "admin_grant": {  # if event has admin_grant, the admin event
            "event_id": str,
            "user_id": str  # the user this admin grant is for
        } | None
    }
}

Output:
{
    "group_members": [row]
}
"""

from pure_projectors.framework import projector, ProjectorResult


@projector(
    event_type="group_member",
    tables=["group_members"]
)
def project(input_dict: dict) -> ProjectorResult:
    """Pure functional projector for group_member events.

    Takes a dict with the event and its resolved dependencies.
    Returns a dict of table rows to insert.
    """
    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]

    # Check group dependency exists
    group = deps.get("group")
    if not group:
        return ProjectorResult(
            blocked=True,
            missing_deps=["group"]
        )

    # Check user dependency exists
    user = deps.get("user")
    if not user:
        return ProjectorResult(
            blocked=True,
            missing_deps=["user"]
        )

    # Authorization check
    admin_grant_id = event_data.get("admin_grant")
    added_by = event_data.get("added_by")  # May be None for legacy events
    group_data = group["event_data"]

    if admin_grant_id:
        # Explicit admin_grant - verify it authorizes the adder
        adder_user = deps.get("adder_user")
        if not adder_user:
            return ProjectorResult(
                blocked=True,
                missing_deps=["adder_user"]
            )

        admin_grant = deps.get("admin_grant")
        if not admin_grant:
            return ProjectorResult(
                blocked=True,
                missing_deps=["admin_grant"]
            )

        # Verify the admin_grant is for the adder's user
        if admin_grant["user_id"] != adder_user["user_id"]:
            return ProjectorResult(
                valid=False,
                reason=f"admin_grant {admin_grant_id} does not authorize adder {adder_user['user_id']}"
            )
    else:
        # Legacy path: adder must be group creator (if added_by is set)
        # This is the "only group creator can add members" rule before admin_grant was added
        # If added_by is None, this is a very old event - accept it
        if added_by and added_by != group_data.get("signed_by"):
            return ProjectorResult(
                valid=False,
                reason=f"adder {added_by} is not group creator {group_data.get('signed_by')} and no admin_grant provided"
            )

    # Build group_member row
    member_row = {
        "member_id": event_id,
        "group_id": event_data["group_id"],
        "user_id": event_data["user_id"],
        "added_by": added_by,
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
        "recorded_at": recorded_at
    }

    return ProjectorResult(
        valid=True,
        tables={
            "group_members": [member_row]
        }
    )
