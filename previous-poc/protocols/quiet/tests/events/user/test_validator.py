"""
Tests for user event type validator.
"""
import pytest
import sys
import time
from pathlib import Path

# Add project root to path
test_dir = Path(__file__).parent
protocol_dir = test_dir.parent.parent.parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from protocols.quiet.events.user.validator import validate


class TestUserValidator:
    """Test user event validation."""

    @pytest.fixture
    def valid_user_event(self):
        """Create a valid user event without invite."""
        peer_id = "a" * 64  # Mock peer ID
        return {
            "event_data": {
                "type": "user",
                "peer_id": peer_id,
                "network_id": "test-network",
                "name": "TestUser",
                "created_at": int(time.time() * 1000),
                "signature": "test-signature"
            },
            "metadata": {
                "peer_id": peer_id
            }
        }

    @pytest.fixture
    def valid_invite_user_event(self):
        """Create a valid user event with invite fields."""
        peer_id = "a" * 64  # Mock peer ID
        return {
            "event_data": {
                "type": "user",
                "peer_id": peer_id,
                "network_id": "test-network",
                "group_id": "test-group",
                "name": "TestUser",
                "invite_pubkey": "b" * 64,
                "invite_signature": "c" * 64,
                "created_at": int(time.time() * 1000),
                "signature": "test-signature"
            },
            "metadata": {
                "peer_id": peer_id
            }
        }

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_valid_user_event(self, valid_user_event):
        """Test validation of a valid user event."""
        envelope = {
            "event_plaintext": valid_user_event["event_data"],
            "peer_id": valid_user_event["metadata"]["peer_id"]
        }

        assert validate(envelope) == True

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_valid_invite_user_event(self, valid_invite_user_event):
        """Test validation of a valid user event with invite."""
        envelope = {
            "event_plaintext": valid_invite_user_event["event_data"],
            "peer_id": valid_invite_user_event["metadata"]["peer_id"],
            "self_created": True  # Invite-based joins are self-created
        }

        assert validate(envelope) == True

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_user_missing_required_fields(self, valid_user_event):
        """Test that missing required fields fail validation."""
        peer_id = valid_user_event["metadata"]["peer_id"]
        required_fields = ['type', 'peer_id', 'network_id', 'name', 'created_at']

        for field in required_fields:
            event_data = valid_user_event["event_data"].copy()
            del event_data[field]
            envelope = {"event_plaintext": event_data, "peer_id": peer_id}
            assert validate(envelope) == False


    @pytest.mark.unit
    @pytest.mark.event_type
    def test_user_peer_id_mismatch(self, valid_user_event):
        """Test that peer_id must match between event and envelope."""
        event_data = valid_user_event["event_data"].copy()
        peer_id = valid_user_event["metadata"]["peer_id"]

        # Change peer_id in event to not match envelope
        event_data["peer_id"] = "b" * 64
        envelope = {"event_plaintext": event_data, "peer_id": peer_id}
        assert validate(envelope) == False

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_user_empty_network_id(self, valid_user_event):
        """Test that empty network_id fails validation."""
        event_data = valid_user_event["event_data"].copy()
        peer_id = valid_user_event["metadata"]["peer_id"]

        # Empty string
        event_data["network_id"] = ""
        envelope = {"event_plaintext": event_data, "peer_id": peer_id}
        assert validate(envelope) == False

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_user_empty_name(self, valid_user_event):
        """Test that empty name fails validation."""
        event_data = valid_user_event["event_data"].copy()
        peer_id = valid_user_event["metadata"]["peer_id"]

        # Empty string
        event_data["name"] = ""
        envelope = {"event_plaintext": event_data, "peer_id": peer_id}
        assert validate(envelope) == False

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_invite_fields_must_both_exist(self, valid_invite_user_event):
        """Test that if one invite field exists, both must exist."""
        peer_id = valid_invite_user_event["metadata"]["peer_id"]

        # Only invite_pubkey
        event_data = valid_invite_user_event["event_data"].copy()
        del event_data["invite_signature"]
        envelope = {"event_plaintext": event_data, "peer_id": peer_id}
        assert validate(envelope) == False

        # Only invite_signature
        event_data = valid_invite_user_event["event_data"].copy()
        del event_data["invite_pubkey"]
        envelope = {"event_plaintext": event_data, "peer_id": peer_id}
        assert validate(envelope) == False

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_invite_requires_group_id(self, valid_invite_user_event):
        """Test that invite-based user events require group_id."""
        event_data = valid_invite_user_event["event_data"].copy()
        peer_id = valid_invite_user_event["metadata"]["peer_id"]

        # Remove group_id from invite-based user event
        del event_data["group_id"]
        envelope = {"event_plaintext": event_data, "peer_id": peer_id}
        assert validate(envelope) == False

        # Empty group_id
        event_data = valid_invite_user_event["event_data"].copy()
        event_data["group_id"] = ""
        envelope = {"event_plaintext": event_data, "peer_id": peer_id}
        assert validate(envelope) == False

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_user_missing_peer_id(self, valid_user_event):
        """Test that missing peer_id in envelope fails validation."""
        event_data = valid_user_event["event_data"].copy()

        # No peer_id in envelope
        envelope = {"event_plaintext": event_data}
        assert validate(envelope) == False

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_user_extra_fields(self, valid_user_event):
        """Test that extra fields are allowed."""
        event_data = valid_user_event["event_data"].copy()
        peer_id = valid_user_event["metadata"]["peer_id"]

        # Add extra fields
        event_data["extra_field"] = "some value"
        event_data["protocol_version"] = 2
        envelope = {"event_plaintext": event_data, "peer_id": peer_id}
        # Extra fields should be allowed
        assert validate(envelope) == True
