"""
Tests for message event type validator.
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

from protocols.quiet.events.message.validator import validate


class TestMessageValidator:
    """Test message event validation."""
    
    @pytest.fixture
    def valid_message_event(self):
        """Create a valid message event envelope."""
        peer_id = "a" * 64  # Mock peer ID
        return {
            "event_plaintext": {
                "type": "message",
                "message_id": "test-message-id",
                "channel_id": "test-channel-id",
                "group_id": "test-group-id", 
                "network_id": "test-network",
                "peer_id": peer_id,
                "content": "Hello, world!",
                "created_at": int(time.time() * 1000),
                "signature": "test-signature"
            },
            "event_type": "message",
            "peer_id": peer_id,
            "network_id": "test-network"
        }
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_valid_message_event(self, valid_message_event):
        """Test validation of a valid message event."""
        assert validate(valid_message_event) == True
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_missing_event_plaintext(self):
        """Test that missing event_plaintext fails validation."""
        envelope = {
            "event_type": "message",
            "peer_id": "test-peer"
        }
        assert validate(envelope) == False
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_missing_peer_id(self, valid_message_event):
        """Message validator does not enforce signer/peer checks; handler covers that."""
        envelope = valid_message_event.copy()
        del envelope["peer_id"]
        assert validate(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_peer_id_mismatch(self, valid_message_event):
        """Mismatch handled by signature/membership checks, not the message validator."""
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        envelope["event_plaintext"]["peer_id"] = "b" * 64
        assert validate(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_empty_content(self, valid_message_event):
        """Test that empty content fails validation."""
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        
        # Empty string
        envelope["event_plaintext"]["content"] = ""
        assert validate(envelope) == False
        
        # Whitespace only
        envelope["event_plaintext"]["content"] = "   \n\t   "
        assert validate(envelope) == False
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_content_too_long(self, valid_message_event):
        """Test that content over 10KB fails validation."""
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        
        # Content over 10000 characters
        envelope["event_plaintext"]["content"] = "A" * 10001
        assert validate(envelope) == False
        
        # Content exactly at limit should pass
        envelope["event_plaintext"]["content"] = "A" * 10000
        assert validate(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_with_special_characters(self, valid_message_event):
        """Test that messages with special characters are valid."""
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        
        # Unicode and emojis
        envelope["event_plaintext"]["content"] = "Hello 👋 world 🌍 with unicode: ñ é ü"
        assert validate(envelope) == True
        
        # Newlines and tabs
        envelope["event_plaintext"]["content"] = "Multi\nline\nmessage\twith\ttabs"
        assert validate(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_single_character(self, valid_message_event):
        """Test that single character messages are valid."""
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        
        envelope["event_plaintext"]["content"] = "!"
        assert validate(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_missing_required_fields(self, valid_message_event):
        """Test that missing required fields fail validation."""
        # Note: The actual required fields check happens in validate_event_data
        # which we're mocking here. This test ensures the validator calls it.
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        
        # Remove type field
        del envelope["event_plaintext"]["type"]
        assert validate(envelope) == False
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_extra_fields(self, valid_message_event):
        """Test that extra fields don't break validation."""
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        
        # Add extra fields
        envelope["event_plaintext"]["edited"] = False
        envelope["event_plaintext"]["reply_to"] = "another-message-id"
        envelope["extra_field"] = "extra_value"
        
        assert validate(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_numeric_content(self, valid_message_event):
        """Test that numeric content as string is valid."""
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        
        envelope["event_plaintext"]["content"] = "12345"
        assert validate(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_message_content_with_leading_whitespace(self, valid_message_event):
        """Test that content with meaningful text after whitespace is valid."""
        envelope = valid_message_event.copy()
        envelope["event_plaintext"] = valid_message_event["event_plaintext"].copy()
        
        # Leading whitespace but has content
        envelope["event_plaintext"]["content"] = "    Hello"
        assert validate(envelope) == True
        
        # Trailing whitespace
        envelope["event_plaintext"]["content"] = "Hello    "
        assert validate(envelope) == True
