"""
Tests for channel event type projector.
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

from protocols.quiet.events.channel.projector import project


class TestChannelProjector:
    """Test channel event projection."""
    
    @pytest.fixture
    def sample_channel_event(self):
        """Create a sample channel event envelope."""
        return {
            'event_plaintext': {
                'type': 'channel',
                'channel_id': 'test-channel-id',
                'group_id': 'test-group-id',
                'name': 'general',
                'network_id': 'test-network',
                'creator_id': 'test-creator',
                'created_at': int(time.time() * 1000),
                'description': 'General discussion'
            },
            'event_type': 'channel',
            'peer_id': 'test-creator',
            'network_id': 'test-network',
            'event_id': 'test-channel-id'
        }
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_channel_creates_deltas(self, sample_channel_event):
        """Test that projecting a channel creates the right deltas."""
        deltas = project(sample_channel_event)
        
        # Should create exactly one delta
        assert len(deltas) == 1
        
        delta = deltas[0]
        assert delta['op'] == 'insert'
        assert delta['table'] == 'channels'
        
        # Check all fields are included
        data = delta['data']
        event_data = sample_channel_event['event_plaintext']
        assert data['channel_id'] == sample_channel_event['event_id']
        assert data['group_id'] == event_data['group_id']
        assert data['network_id'] == event_data['network_id']
        assert data['name'] == event_data['name']
        assert data['creator_id'] == event_data['creator_id']
        assert data['created_at'] == event_data['created_at']
        assert data['description'] == event_data['description']
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_channel_no_description(self):
        """Test projecting channel without description."""
        envelope = {
            'event_plaintext': {
                'type': 'channel',
                'channel_id': 'test-channel-2',
                'group_id': 'test-group-id',
                'name': 'random',
                'network_id': 'test-network',
                'creator_id': 'test-creator',
                'created_at': int(time.time() * 1000)
                # No description field
            }
        }
        envelope['event_id'] = 'test-channel-2'
        
        deltas = project(envelope)
        
        # Description should default to empty string
        assert deltas[0]['data']['description'] == ''
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_channel_preserves_all_fields(self, sample_channel_event):
        """Test that all fields are preserved in projection."""
        # Add some extra fields
        sample_channel_event['event_plaintext']['extra_field'] = 'extra_value'
        
        deltas = project(sample_channel_event)
        
        # Standard fields should be preserved
        data = deltas[0]['data']
        assert 'channel_id' in data
        assert 'group_id' in data
        assert 'network_id' in data
        assert 'name' in data
        assert 'creator_id' in data
        assert 'created_at' in data
        assert 'description' in data
        
        # Extra fields are not included in the projection
        assert 'extra_field' not in data
    
    @pytest.mark.unit
    @pytest.mark.event_type  
    def test_project_channel_delta_structure(self, sample_channel_event):
        """Test the structure of the delta returned."""
        deltas = project(sample_channel_event)
        
        delta = deltas[0]
        
        # Check delta has required fields
        assert 'op' in delta
        assert 'table' in delta
        assert 'data' in delta
        
        # Check operation type
        assert delta['op'] == 'insert'
        assert delta['table'] == 'channels'
        
        # Data should be a dict
        assert isinstance(delta['data'], dict)
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_multiple_channels(self):
        """Test projecting multiple different channels."""
        # First channel
        envelope1 = {
            'event_plaintext': {
                'type': 'channel',
                'channel_id': 'channel-1',
                'group_id': 'group-1',
                'name': 'general',
                'network_id': 'network-1',
                'creator_id': 'creator-1',
                'created_at': 1000
            }
        }
        envelope1['event_id'] = 'channel-1'
        
        # Second channel
        envelope2 = {
            'event_plaintext': {
                'type': 'channel',
                'channel_id': 'channel-2',
                'group_id': 'group-1',
                'name': 'random',
                'network_id': 'network-1',
                'creator_id': 'creator-1',
                'created_at': 2000
            }
        }
        envelope2['event_id'] = 'channel-2'
        
        deltas1 = project(envelope1)
        deltas2 = project(envelope2)
        
        # Each should produce one delta
        assert len(deltas1) == 1
        assert len(deltas2) == 1
        
        # Different channel IDs
        assert deltas1[0]['data']['channel_id'] != deltas2[0]['data']['channel_id']
        
        # Same group
        assert deltas1[0]['data']['group_id'] == deltas2[0]['data']['group_id']
