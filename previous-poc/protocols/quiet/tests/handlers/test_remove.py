"""
Tests for remove handler.
"""
import pytest
from protocols.quiet.handlers.remove import (
    filter_func, handler, is_explicitly_deleted, get_removal_context, get_remover
)
from protocols.quiet.tests.handlers.test_base import HandlerTestBase


class TestRemoveHandler(HandlerTestBase):
    """Test the remove handler."""
    
    def setup_method(self):
        """Set up additional tables for remove handler."""
        super().setup_method()
        
        # Add test data for removal
        self.db.execute("""
            INSERT INTO deleted_events (event_id, deleted_at, deleted_by, reason)
            VALUES (?, ?, ?, ?)
        """, ("deleted_event_123", 1000, "admin", "spam"))
        
        self.db.execute("""
            INSERT INTO deleted_channels (channel_id, deleted_at, deleted_by)
            VALUES (?, ?, ?)
        """, ("deleted_channel_456", 2000, "moderator"))
        
        self.db.commit()
    
    def test_filter_skips_already_marked_keep(self):
        """Test filter skips events already marked should_remove=false."""
        envelope = self.create_envelope(
            event_id="test",
            should_remove=False
        )
        assert filter_func(envelope) is False
    
    def test_filter_accepts_event_id_only(self):
        """Test filter accepts early check with just event_id."""
        envelope = self.create_envelope(
            event_id="test"
        )
        assert filter_func(envelope) is True
    
    def test_filter_accepts_content_check(self):
        """Test filter accepts content check with plaintext."""
        envelope = self.create_envelope(
            event_plaintext={"type": "message"},
            event_type="message"
        )
        assert filter_func(envelope) is True
    
    def test_filter_accepts_both_phases(self):
        """Test filter accepts envelope with both ID and content."""
        envelope = self.create_envelope(
            event_id="test",
            event_plaintext={"type": "message"},
            event_type="message"
        )
        assert filter_func(envelope) is True
    
    def test_handler_removes_explicitly_deleted(self):
        """Test handler removes explicitly deleted events."""
        envelope = self.create_envelope(
            event_id="deleted_event_123"
        )
        
        result = handler(envelope, self.db)
        
        assert result is None  # Dropped
    
    def test_handler_keeps_non_deleted(self):
        """Test handler keeps events not explicitly deleted."""
        envelope = self.create_envelope(
            event_id="not_deleted"
        )
        
        result = handler(envelope, self.db)
        
        assert result is not None
        assert result['should_remove'] is False
    
    def test_handler_type_specific_removal(self):
        """Test handler calls type-specific removers."""
        # Create a mock remover module
        class MockRemover:
            @staticmethod
            def should_remove(envelope, context):
                # Remove if in deleted channel
                channel_id = envelope.get('event_plaintext', {}).get('channel_id')
                return channel_id in context['deleted_channels']
        
        # Monkey patch the remover cache
        import protocols.quiet.handlers.remove as rm
        rm._removers_cache['message'] = MockRemover
        
        # Test removal
        envelope = self.create_envelope(
            event_id="msg_in_deleted_channel",
            event_plaintext={"channel_id": "deleted_channel_456"},
            event_type="message"
        )
        
        result = handler(envelope, self.db)
        
        assert result is None  # Should be removed
        
        # Test non-removal
        envelope = self.create_envelope(
            event_id="msg_in_active_channel",
            event_plaintext={"channel_id": "active_channel"},
            event_type="message"
        )
        
        result = handler(envelope, self.db)
        
        assert result is not None
        assert result['should_remove'] is False
    
    def test_is_explicitly_deleted(self):
        """Test explicit deletion check."""
        assert is_explicitly_deleted("deleted_event_123", self.db) is True
        assert is_explicitly_deleted("not_deleted", self.db) is False
    
    def test_get_removal_context(self):
        """Test removal context gathering."""
        context = get_removal_context(self.db)
        
        assert 'deleted_channels' in context
        assert 'removed_users' in context
        assert 'deleted_messages' in context
        
        assert 'deleted_channel_456' in context['deleted_channels']
        assert 'deleted_event_123' in context['deleted_messages']
    
    def test_get_remover_caching(self):
        """Test remover module caching."""
        # First call should cache
        remover1 = get_remover('test_type')
        
        # Second call should return cached
        remover2 = get_remover('test_type')
        
        # For non-existent removers, both should be None
        assert remover1 is None
        assert remover2 is None
        
        # Cache should contain the entry
        import protocols.quiet.handlers.remove as rm
        assert 'test_type' in rm._removers_cache
    
    def test_handler_handles_remover_errors(self):
        """Test handler handles errors in removers gracefully."""
        # Create a broken remover
        class BrokenRemover:
            @staticmethod
            def should_remove(envelope, context):
                raise ValueError("Remover error")
        
        import protocols.quiet.handlers.remove as rm
        rm._removers_cache['broken'] = BrokenRemover
        
        envelope = self.create_envelope(
            event_id="test",
            event_plaintext={"type": "broken"},
            event_type="broken"
        )
        
        # Should not crash, should keep event
        result = handler(envelope, self.db)
        
        assert result is not None
        assert result['should_remove'] is False