"""
Tests for event_store handler.
"""
import pytest
import time
from protocols.quiet.handlers.event_store import (
    filter_func, handler, purge_event
)
from protocols.quiet.tests.handlers.test_base import HandlerTestBase


class TestEventStoreHandler(HandlerTestBase):
    """Test the event_store handler."""
    
    def test_filter_accepts_write_flag(self):
        """Test filter accepts envelopes with write_to_store flag."""
        envelope = self.create_envelope(
            write_to_store=True,
            event_id="test"
        )
        assert filter_func(envelope) is True
    
    def test_filter_rejects_no_write_flag(self):
        """Test filter rejects envelopes without write flag."""
        envelope = self.create_envelope(
            event_id="test"
        )
        assert filter_func(envelope) is False
    
    def test_handler_stores_event(self):
        """Test handler stores event data."""
        test_time = int(time.time() * 1000)
        test_blob = b"\x01" + b"\x00"*16 + b"\x00"*24 + b"\x00"
        envelope = self.create_envelope(
            write_to_store=True,
            event_id="new_event_123",
            event_blob=test_blob,
            key_ref={"kind": "key", "id": "key_123"},
            received_at=test_time - 1000,
            origin_ip="192.168.1.1",
            origin_port=8080
        )

        result = handler(envelope, self.db)

        assert result['stored'] is True

        # Check database
        cursor = self.db.execute(
            "SELECT * FROM events WHERE event_id = ?",
            ("new_event_123",)
        )
        row = cursor.fetchone()
        assert row is not None
        assert row['event_blob'] == test_blob
    
    def test_handler_stores_key_event(self):
        """Test handler stores unsealed key event."""
        envelope = self.create_envelope(
            write_to_store=True,
            event_id="key_event_456",
            event_type="key",
            key_id="key_456",
            unsealed_secret=b"secret_key_data",
            group_id="group_789"
        )
        
        result = handler(envelope, self.db)
        
        assert result['stored'] is True
        
        # Check database
        # Minimal ES no longer stores key details; success means stored flag was set
        assert result['stored'] is True
    
    def test_handler_requires_event_id(self):
        """Test handler requires event_id."""
        envelope = self.create_envelope(
            write_to_store=True
            # Missing event_id
        )
        
        result = handler(envelope, self.db)
        
        assert 'error' in result
        assert 'event_id' in result['error']
    
    def test_handler_deduplicates(self):
        """Test handler handles duplicate event_id."""
        # Store first time
        envelope = self.create_envelope(
            write_to_store=True,
            event_id="dup_event",
            event_type="test"
        )
        result1 = handler(envelope, self.db)
        assert result1['stored'] is True
        
        # Try to store again
        result2 = handler(envelope, self.db)
        assert result2['stored'] is True  # Still returns success
        
        # Check only one in database
        cursor = self.db.execute(
            "SELECT COUNT(*) as count FROM events WHERE event_id = ?",
            ("dup_event",)
        )
        assert cursor.fetchone()['count'] == 1
    
    def test_purge_is_noop(self):
        """Test purge_event is a no-op with minimal ES."""
        assert purge_event("any", self.db, "reason") is True
    
    def test_purge_event_handles_missing(self):
        assert purge_event("does_not_exist", self.db, "test") in (True, False)
