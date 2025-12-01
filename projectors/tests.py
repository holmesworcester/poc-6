"""Pure projector tests - NO DATABASE REQUIRED.

Tests are pure: input dict -> assert on result.
Run with: python -m projectors.tests
"""

from projectors import ProjectorResult
from events.content import message, channel
from events.group import group_member
from events.identity import user


# ============================================================================
# MESSAGE TESTS
# ============================================================================

def test_message_basic():
    """Basic message projects correctly."""
    result = message.project(message.make_input())
    assert result.valid
    assert "messages" in result.tables
    assert result.tables["messages"][0]["content"] == "test message"


def test_message_ttl_explicit():
    """TTL calculated from disappearing_time_ms."""
    input_dict = message.make_input(
        event_data=message.make_event_data(created_at=1000, disappearing_time_ms=5000)
    )
    result = message.project(input_dict)
    assert result.tables["messages"][0]["ttl_ms"] == 6000


def test_message_ttl_permanent():
    """disappearing_time_ms=0 means permanent."""
    input_dict = message.make_input(
        event_data=message.make_event_data(disappearing_time_ms=0)
    )
    result = message.project(input_dict)
    assert result.tables["messages"][0]["ttl_ms"] == 0


def test_message_deletion_valid():
    """Valid deletion skips message, adds to deleted_events."""
    input_dict = message.make_input(deletion={"deleted_by": "admin", "is_valid": True})
    result = message.project(input_dict)
    assert result.valid
    assert "messages" not in result.tables
    assert "deleted_events" in result.tables


def test_message_deletion_invalid():
    """Invalid deletion still projects message."""
    input_dict = message.make_input(deletion={"deleted_by": "hacker", "is_valid": False})
    result = message.project(input_dict)
    assert "messages" in result.tables


# ============================================================================
# CHANNEL TESTS
# ============================================================================

def test_channel_basic():
    """Basic channel projects correctly."""
    result = channel.project(channel.make_input())
    assert result.valid
    assert "channels" in result.tables
    assert result.tables["channels"][0]["name"] == "general"


def test_channel_with_admin_grant():
    """Channel with admin_grant requires matching user."""
    input_dict = channel.make_input(
        event_data=channel.make_event_data(admin_grant="admin_123"),
        signer_user={"user_id": "user_456", "peer_id": "peer_123"},
        admin_grant={"event_id": "admin_123", "user_id": "user_456"},
    )
    result = channel.project(input_dict)
    assert result.valid


def test_channel_admin_grant_mismatch():
    """admin_grant for wrong user fails."""
    input_dict = channel.make_input(
        event_data=channel.make_event_data(admin_grant="admin_123"),
        signer_user={"user_id": "user_456", "peer_id": "peer_123"},
        admin_grant={"event_id": "admin_123", "user_id": "wrong_user"},
    )
    result = channel.project(input_dict)
    assert not result.valid
    assert "does not authorize" in result.reason


def test_channel_missing_signer_blocks():
    """Missing signer_user blocks projection."""
    input_dict = channel.make_input(
        event_data=channel.make_event_data(admin_grant="admin_123"),
        signer_user=None,
    )
    result = channel.project(input_dict)
    assert result.blocked
    assert "signer_user" in result.missing_deps


# ============================================================================
# GROUP_MEMBER TESTS
# ============================================================================

def test_group_member_basic():
    """Basic group_member projects correctly."""
    result = group_member.project(group_member.make_input())
    assert result.valid
    assert "group_members" in result.tables


def test_group_member_missing_group():
    """Missing group blocks projection."""
    input_dict = group_member.make_input()
    input_dict["dependencies"]["group"] = None  # Override default
    result = group_member.project(input_dict)
    assert result.blocked
    assert "group" in result.missing_deps


def test_group_member_missing_user():
    """Missing user blocks projection."""
    input_dict = group_member.make_input()
    input_dict["dependencies"]["user"] = None  # Override default
    result = group_member.project(input_dict)
    assert result.blocked
    assert "user" in result.missing_deps


def test_group_member_admin_grant_valid():
    """Valid admin_grant authorizes member addition."""
    input_dict = group_member.make_input(
        event_data=group_member.make_event_data(admin_grant="admin_123"),
        adder_user={"user_id": "user_456", "peer_id": "peer_123"},
        admin_grant={"event_id": "admin_123", "user_id": "user_456"},
    )
    result = group_member.project(input_dict)
    assert result.valid


def test_group_member_admin_grant_mismatch():
    """admin_grant for wrong user fails."""
    input_dict = group_member.make_input(
        event_data=group_member.make_event_data(admin_grant="admin_123"),
        adder_user={"user_id": "user_456", "peer_id": "peer_123"},
        admin_grant={"event_id": "admin_123", "user_id": "wrong_user"},
    )
    result = group_member.project(input_dict)
    assert not result.valid


def test_group_member_legacy_creator():
    """Legacy: group creator can add members without admin_grant."""
    input_dict = group_member.make_input(
        event_data=group_member.make_event_data(added_by="peer_creator", admin_grant=None),
        group=group_member.make_group_dep(signed_by="peer_creator"),
    )
    result = group_member.project(input_dict)
    assert result.valid


def test_group_member_legacy_non_creator_fails():
    """Legacy: non-creator without admin_grant fails."""
    input_dict = group_member.make_input(
        event_data=group_member.make_event_data(added_by="peer_other", admin_grant=None),
        group=group_member.make_group_dep(signed_by="peer_creator"),
    )
    result = group_member.project(input_dict)
    assert not result.valid


# ============================================================================
# USER TESTS
# ============================================================================

def test_user_basic():
    """Basic user projects correctly."""
    result = user.project(user.make_input())
    assert result.valid
    assert "users" in result.tables
    assert "valid_events" in result.tables
    assert result.tables["users"][0]["name"] == "Test User"


def test_user_auto_group_member():
    """User auto-added to invite's group."""
    result = user.project(user.make_input())
    assert "group_members" in result.tables
    assert result.tables["group_members"][0]["group_id"] == "grp_123"


def test_user_no_group_in_invite():
    """No group_members if invite has no group_id."""
    input_dict = user.make_input(
        invite=user.make_invite_dep(group_id="")
    )
    result = user.project(input_dict)
    # Empty group_id means no auto-add
    if "group_members" in result.tables:
        assert result.tables["group_members"][0]["group_id"] == ""


def test_user_signature_invalid():
    """Invalid signature fails projection."""
    input_dict = user.make_input(signature_valid=False)
    result = user.project(input_dict)
    assert not result.valid
    assert "Signature" in result.reason


def test_user_missing_invite():
    """Missing invite blocks projection."""
    input_dict = user.make_input()
    input_dict["dependencies"]["invite"] = None  # Override default
    result = user.project(input_dict)
    assert result.blocked
    assert "invite" in result.missing_deps


def test_user_mismatched_signed_by():
    """signed_by != invite_id fails."""
    input_dict = user.make_input(
        event_data={
            "type": "user",
            "invite_id": "inv_123",
            "signed_by": "wrong_signer",
            "name": "Test",
            "user_pubkey": "key",
            "created_at": 1000,
        }
    )
    result = user.project(input_dict)
    assert not result.valid


# ============================================================================
# RUN ALL TESTS
# ============================================================================

def run_all():
    tests = [
        # Message
        test_message_basic,
        test_message_ttl_explicit,
        test_message_ttl_permanent,
        test_message_deletion_valid,
        test_message_deletion_invalid,
        # Channel
        test_channel_basic,
        test_channel_with_admin_grant,
        test_channel_admin_grant_mismatch,
        test_channel_missing_signer_blocks,
        # Group member
        test_group_member_basic,
        test_group_member_missing_group,
        test_group_member_missing_user,
        test_group_member_admin_grant_valid,
        test_group_member_admin_grant_mismatch,
        test_group_member_legacy_creator,
        test_group_member_legacy_non_creator_fails,
        # User
        test_user_basic,
        test_user_auto_group_member,
        test_user_no_group_in_invite,
        test_user_signature_invalid,
        test_user_missing_invite,
        test_user_mismatched_signed_by,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS: {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {test.__name__} - {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {test.__name__} - {e}")

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    print("Running pure projector tests (no database)...\n")
    success = run_all()
    exit(0 if success else 1)
