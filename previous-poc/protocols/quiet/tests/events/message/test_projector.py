"""
Tests for message event type projector.
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

from protocols.quiet.events.message.projector import project


class TestMessageProjector:
    """Test message event projection."""
    
    @pytest.fixture
    def sample_message_event(self):
        """Create a sample message event envelope."""
        return {
            'event_plaintext': {
                'type': 'message',
                'message_id': 'test-message-id',
                'channel_id': 'test-channel-id',
                'group_id': 'test-group-id',
                'network_id': 'test-network',
                'peer_id': 'test-author',
                'content': 'Hello, world!',
                'created_at': int(time.time() * 1000),
                'signature': 'test-signature'
            },
            'event_type': 'message',
            'event_id': 'test-event-id',
            'peer_id': 'test-author',
            'network_id': 'test-network',
            'sig_checked': True,
            'event_type': 'message',
            'event_id': 'test-event-id',
            'peer_id': 'test-author',
            'sig_checked': True,
            'validated': True
        }
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_message_creates_deltas(self, sample_message_event):
        """Test that projecting a message creates the right deltas."""
        deltas = project(sample_message_event)
        
        # Should create exactly one delta
        assert len(deltas) == 1
        
        delta = deltas[0]
        assert delta['op'] == 'insert'
        assert delta['table'] == 'messages'
        assert 'where' in delta  # Required field for Delta type
        
        # Check all fields are included
        data = delta['data']
        event_data = sample_message_event['event_plaintext']
        assert data['message_id'] == sample_message_event['event_id']
        assert data['channel_id'] == event_data['channel_id']
        assert data['group_id'] == event_data['group_id']
        assert data['network_id'] == event_data['network_id']
        assert data['author_id'] == event_data['peer_id']  # peer_id becomes author_id
        assert data['content'] == event_data['content']
        assert data['created_at'] == event_data['created_at']
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_message_peer_id_to_author_id(self, sample_message_event):
        """Test that peer_id is mapped to author_id in projection."""
        deltas = project(sample_message_event)
        
        data = deltas[0]['data']
        # peer_id from event becomes author_id in database
        assert data['author_id'] == sample_message_event['event_plaintext']['peer_id']
        assert 'peer_id' not in data  # Should not include peer_id directly
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_message_empty_content(self):
        """Test projecting message with empty content."""
        envelope = {
            'event_plaintext': {
                'type': 'message',
                'message_id': 'empty-message',
                'channel_id': 'test-channel',
                'group_id': 'test-group',
                'network_id': 'test-network',
                'peer_id': 'test-author',
                'content': '',  # Empty content
                'created_at': int(time.time() * 1000),
                'signature': 'test-signature'
            },
            'event_type': 'message',
            'event_id': 'test-event-id',
            'peer_id': 'test-author',
            'sig_checked': True,
            'validated': True
        }
        
        deltas = project(envelope)
        
        # Empty content should be preserved
        assert deltas[0]['data']['content'] == ''
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_message_special_characters(self):
        """Test projecting message with special characters."""
        envelope = {
            'event_plaintext': {
                'type': 'message',
                'message_id': 'special-message',
                'channel_id': 'test-channel',
                'group_id': 'test-group',
                'network_id': 'test-network',
                'peer_id': 'test-author',
                'content': 'Hello 👋\nWorld 🌍\tWith unicode: ñ é ü',
                'created_at': int(time.time() * 1000),
                'signature': 'test-signature'
            },
            'event_type': 'message',
            'event_id': 'test-event-id',
            'peer_id': 'test-author',
            'sig_checked': True,
            'validated': True
        }
        
        deltas = project(envelope)
        
        # Special characters should be preserved
        assert deltas[0]['data']['content'] == 'Hello 👋\nWorld 🌍\tWith unicode: ñ é ü'
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_message_delta_structure(self, sample_message_event):
        """Test the structure of the delta returned."""
        deltas = project(sample_message_event)
        
        delta = deltas[0]
        
        # Check delta has required fields for Delta type
        assert 'op' in delta
        assert 'table' in delta
        assert 'data' in delta
        assert 'where' in delta
        
        # Check operation type
        assert delta['op'] == 'insert'
        assert delta['table'] == 'messages'
        assert delta['where'] == {}  # Empty for insert
        
        # Data should be a dict
        assert isinstance(delta['data'], dict)
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_message_invalid_envelope(self):
        """Test that invalid envelope returns empty deltas."""
        # Missing validated field - will fail cast_envelope
        envelope = {
            'event_plaintext': {
                'type': 'message',
                'message_id': 'test-message'
            }
        }
        
        deltas = project(envelope)
        assert deltas == []
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_multiple_messages(self):
        """Test projecting multiple different messages."""
        # First message
        envelope1 = {
            'event_plaintext': {
                'type': 'message',
                'message_id': 'msg-1',
                'channel_id': 'channel-1',
                'group_id': 'group-1',
                'network_id': 'network-1',
                'peer_id': 'author-1',
                'content': 'First message',
                'created_at': 1000,
                'signature': 'sig-1'
            },
            'event_type': 'message',
            'event_id': 'msg-1',
            'peer_id': 'test-author',
            'sig_checked': True,
            'validated': True
        }
        
        # Second message
        envelope2 = {
            'event_plaintext': {
                'type': 'message',
                'message_id': 'msg-2',
                'channel_id': 'channel-1',
                'group_id': 'group-1',
                'network_id': 'network-1',
                'peer_id': 'author-2',
                'content': 'Second message',
                'created_at': 2000,
                'signature': 'sig-2'
            },
            'event_type': 'message',
            'event_id': 'msg-2',
            'peer_id': 'test-author',
            'sig_checked': True,
            'validated': True
        }
        
        deltas1 = project(envelope1)
        deltas2 = project(envelope2)
        
        # Each should produce one delta
        assert len(deltas1) == 1
        assert len(deltas2) == 1
        
        # Different message IDs
        assert deltas1[0]['data']['message_id'] != deltas2[0]['data']['message_id']
        
        # Different authors
        assert deltas1[0]['data']['author_id'] != deltas2[0]['data']['author_id']
        
        # Same channel
        assert deltas1[0]['data']['channel_id'] == deltas2[0]['data']['channel_id']
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_message_preserves_timestamps(self, sample_message_event):
        """Test that timestamps are preserved correctly."""
        created_at = sample_message_event['event_plaintext']['created_at']
        
        deltas = project(sample_message_event)
        
        # Creation time should be preserved
        assert deltas[0]['data']['created_at'] == created_at
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_message_no_extra_fields(self, sample_message_event):
        """Test that only expected fields are included in projection."""
        deltas = project(sample_message_event)
        
        data = deltas[0]['data']
        
        # Check only expected fields are present
        expected_fields = {
            'message_id', 'channel_id', 'group_id', 'network_id',
            'author_id', 'content', 'created_at'
        }
        
        assert set(data.keys()) == expected_fields
        
        # Signature should not be included in projection
        assert 'signature' not in data
