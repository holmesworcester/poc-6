"""
Tests for project handler.
"""
import pytest
import sys
from pathlib import Path
from typing import Dict, Any

# Add project root to path
protocol_dir = Path(__file__).parent.parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from protocols.quiet.handlers.project import ProjectHandler


class TestProjectHandler:
    """Test projection handler."""
    
    @pytest.fixture
    def handler(self):
        """Create handler instance."""
        return ProjectHandler()
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_filter_requires_validated(self, handler):
        """Test filter requires validated to be true."""
        envelope: Dict[str, Any] = {
            "event_type": "message",
            "event_plaintext": {"type": "message", "channel_id": "c1", "peer_id": "p1", "content": "hi", "created_at": 1},
            "event_id": "e1"
        }
        assert handler.filter(envelope) == False
        
        envelope["validated"] = False
        assert handler.filter(envelope) == False
        
        envelope["validated"] = True
        assert handler.filter(envelope) == True
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_filter_skips_projected(self, handler):
        """Test filter skips already projected envelopes."""
        envelope: Dict[str, Any] = {
            "event_type": "identity",
            "event_plaintext": {"type": "peer_secret"},
            "validated": True,
            "projected": True
        }
        assert handler.filter(envelope) == False
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_process_message_event(self, handler, initialized_db):
        """Test projecting a message event inserts into messages table."""
        # Seed channel row that projector expects relationships for
        initialized_db.execute(
            "INSERT INTO channels (channel_id, name, group_id, network_id, creator_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("chan-1", "general", "group-1", "net-1", "creator", 1)
        )
        envelope = {
            "event_plaintext": {
                "type": "message",
                "channel_id": "chan-1",
                "group_id": "group-1",
                "network_id": "net-1",
                "peer_id": "author-1",
                "created_by": "author-1",
                "content": "Hello",
                "created_at": 2
            },
            "event_type": "message",
            "event_id": "msg-1",
            "validated": True
        }
        results = handler.process(envelope, initialized_db)
        # No unblock envelopes
        assert results == []
        # Check DB
        row = initialized_db.execute("SELECT * FROM messages WHERE message_id = ?", ("msg-1",)).fetchone()
        assert row is not None
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_process_key_event(self, handler, sample_key_event, initialized_db):
        """Test projecting key event."""
        envelope = {
            "event_plaintext": sample_key_event,
            "event_type": "key",
            "event_id": "key-1",
            "validated": True
        }
        results = handler.process(envelope, initialized_db)
        assert results == []
    
    # transit_secret removed in favor of per-request transit secrets in sync_request
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_process_unknown_event_type(self, handler, initialized_db):
        """Test projecting unknown event type."""
        envelope = {
            "event_plaintext": {"type": "unknown"},
            "event_type": "unknown",
            "event_id": "x",
            "validated": True
        }
        results = handler.process(envelope, initialized_db)
        # No projector for unknown type: expect no emission
        assert results == []
    
    @pytest.mark.unit
    @pytest.mark.handler
    def test_process_preserves_envelope(self, handler, initialized_db):
        """Test that handler preserves envelope data."""
        envelope = {
            "event_plaintext": {"type": "channel", "channel_id": "c1", "group_id": "g1", "network_id": "n1", "creator_id": "p1", "name": "general", "created_at": 1},
            "event_type": "channel",
            "event_id": "c1",
            "validated": True,
            "custom_field": "preserved"
        }
        results = handler.process(envelope, initialized_db)
        assert results == []
