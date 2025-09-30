"""
Tests for validate handler.
"""
import pytest
import sys
from pathlib import Path

# Add project root to path
protocol_dir = Path(__file__).parent.parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from protocols.quiet.handlers.validate import ValidateHandler


class TestValidateHandler:
    """Test validation handler."""
    
    @pytest.fixture
    def handler(self):
        """Create handler instance."""
        return ValidateHandler()
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_filter_requires_sig_checked(self, handler):
        """Test filter requires sig_checked to be true."""
        envelope = {
            "event_type": "message",
            "event_plaintext": {"type": "message", "message_id": "m1", "channel_id": "c1", "group_id": "g1", "network_id": "n1", "peer_id": "p1", "content": "hi", "created_at": 1, "signature": "sig"},
            "deps_included_and_valid": True,
            "self_created": True
        }
        assert handler.filter(envelope) == False
        
        envelope["sig_checked"] = False
        assert handler.filter(envelope) == False
        
        envelope["sig_checked"] = True
        assert handler.filter(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_filter_skips_validated(self, handler):
        """Test filter skips already validated envelopes."""
        envelope = {
            "event_type": "message",
            "event_plaintext": {"type": "message", "channel_id": "c1", "peer_id": "p1", "content": "hi", "created_at": 1},
            "deps_included_and_valid": True,
            "sig_checked": True,
            "validated": True
        }
        assert handler.filter(envelope) == False
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_filter_requires_event_plaintext(self, handler):
        """Test filter requires event_plaintext."""
        envelope = {"event_type": "message", "deps_included_and_valid": True, "sig_checked": True}
        assert handler.filter(envelope) == False
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_process_valid_message(self, handler, initialized_db):
        """Test processing valid message event."""
        envelope = {
            "event_plaintext": {"type": "message", "message_id": "m1", "channel_id": "c1", "group_id": "g1", "network_id": "n1", "created_by": "p1", "content": "hi", "created_at": 1, "signature": "sig"},
            "event_type": "message",
            "deps_included_and_valid": True,
            "sig_checked": True,
            "self_created": True
        }
        
        results = handler.process(envelope, initialized_db)
        
        assert len(results) == 1
        result = results[0]
        
        # Should mark as validated
        assert result["validated"] == True
        assert "error" not in result
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_process_invalid_message(self, handler, initialized_db):
        """Test processing invalid message event."""
        event = {"type": "message", "message_id": "m1", "channel_id": "c1", "group_id": "g1", "network_id": "n1", "created_by": "p1", "content": "", "created_at": 1, "signature": "sig"}
        envelope = {"event_plaintext": event, "event_type": "message", "deps_included_and_valid": True, "sig_checked": True, "self_created": True}
        
        results = handler.process(envelope, initialized_db)
        # Invalid events are dropped by validate handler
        assert len(results) == 0
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_process_unknown_event_type(self, handler, initialized_db):
        """Test processing unknown event type."""
        envelope = {"event_plaintext": {"type": "unknown"}, "event_type": "unknown", "deps_included_and_valid": True, "sig_checked": True}
        results = handler.process(envelope, initialized_db)
        assert len(results) == 0
    
    @pytest.mark.unit
    @pytest.mark.handler
    @pytest.mark.unit
    @pytest.mark.handler
    def test_process_preserves_envelope_data(self, handler, initialized_db):
        envelope = {"event_plaintext": {"type": "message", "message_id": "m1", "channel_id": "c1", "group_id": "g1", "network_id": "n1", "created_by": "p1", "content": "hi", "created_at": 1, "signature": "sig"}, "event_type": "message", "deps_included_and_valid": True, "sig_checked": True, "custom_field": "preserved", "self_created": True}
        results = handler.process(envelope, initialized_db)
        result = results[0]
        assert result["custom_field"] == "preserved"
        assert result["validated"] is True

    @pytest.mark.unit
    @pytest.mark.handler
    def test_user_peer_match_validates(self, handler, initialized_db):
        """User validator should require envelope.peer_id to match event.peer_id."""
        user_event = {
            "type": "user",
            "peer_id": "p-user-1",
            "network_id": "net-1",
            "name": "Alice",
            "created_at": 1,
            "signature": "sig"
        }
        envelope = {
            "event_plaintext": user_event,
            "event_type": "user",
            "peer_id": "p-user-1",
            "deps_included_and_valid": True,
            "sig_checked": True,
            "self_created": True
        }
        results = handler.process(envelope, initialized_db)
        assert len(results) == 1
        assert results[0]["validated"] is True

    @pytest.mark.unit
    @pytest.mark.handler
    def test_user_peer_mismatch_fails(self, handler, initialized_db):
        user_event = {
            "type": "user",
            "peer_id": "p-user-1",
            "network_id": "net-1",
            "name": "Alice",
            "created_at": 1,
            "signature": "sig"
        }
        envelope = {
            "event_plaintext": user_event,
            "event_type": "user",
            "peer_id": "different-peer",
            "deps_included_and_valid": True,
            "sig_checked": True
        }
        results = handler.process(envelope, initialized_db)
        # Mismatch -> dropped by validate
        assert len(results) == 0
