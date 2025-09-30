"""
Tests for group event type projector.
"""
import pytest
import sys
import time
import json
from pathlib import Path

# Add project root to path
test_dir = Path(__file__).parent
protocol_dir = test_dir.parent.parent.parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from protocols.quiet.events.group.projector import project


class TestGroupProjector:
    """Test group event projection."""
    
    @pytest.fixture
    def sample_group_event(self):
        """Create a sample group event envelope."""
        return {
            'event_plaintext': {
                'type': 'group',
                'group_id': 'test-group-id',
                'name': 'Engineering',
                'network_id': 'test-network',
                'creator_id': 'test-creator',
                'created_at': int(time.time() * 1000),
                'permissions': {
                    'invite': ['creator', 'admin'],
                    'remove': ['creator', 'admin'],
                    'message': ['all']
                }
            },
            'event_type': 'group',
            'event_id': 'test-group-id',  # The projector uses this as group_id
            'peer_id': 'test-creator',
            'network_id': 'test-network'
        }
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_group_creates_deltas(self, sample_group_event):
        """Test that projecting a group creates the right deltas."""
        deltas = project(sample_group_event)
        
        # Should create exactly two deltas - group and membership
        assert len(deltas) == 2
        
        # First delta - create group
        group_delta = deltas[0]
        assert group_delta['op'] == 'insert'
        assert group_delta['table'] == 'groups'
        
        # Check all group fields
        group_data = group_delta['data']
        event_data = sample_group_event['event_plaintext']
        assert group_data['group_id'] == event_data['group_id']
        assert group_data['network_id'] == event_data['network_id']
        assert group_data['name'] == event_data['name']
        assert group_data['creator_id'] == event_data['creator_id']
        assert group_data['owner_id'] == event_data['creator_id']
        assert group_data['created_at'] == event_data['created_at']
        assert group_data['permissions'] == json.dumps(event_data['permissions'])
        
        # Second delta - add creator as member
        member_delta = deltas[1]
        assert member_delta['op'] == 'insert'
        assert member_delta['table'] == 'group_members'
        
        # Check membership fields
        member_data = member_delta['data']
        assert member_data['group_id'] == event_data['group_id']
        assert member_data['user_id'] == event_data['creator_id']
        assert member_data['added_by'] == event_data['creator_id']
        assert member_data['added_at'] == event_data['created_at']
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_group_no_permissions(self):
        """Test projecting group without permissions."""
        envelope = {
            'event_plaintext': {
                'type': 'group',
                'group_id': 'test-group-2',
                'name': 'Marketing',
                'network_id': 'test-network',
                'creator_id': 'test-creator',
                'created_at': int(time.time() * 1000)
                # No permissions field
            },
            'event_type': 'group',
            'event_id': 'test-group-2',  # The projector uses this as group_id
            'peer_id': 'test-creator',
            'network_id': 'test-network'
        }

        deltas = project(envelope)
        
        # Permissions should default to empty dict
        group_delta = deltas[0]
        assert group_delta['data']['permissions'] == json.dumps({})
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_group_creator_is_owner(self, sample_group_event):
        """Test that creator becomes the owner."""
        deltas = project(sample_group_event)
        
        group_data = deltas[0]['data']
        assert group_data['owner_id'] == group_data['creator_id']
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_group_membership_auto_added(self, sample_group_event):
        """Test that creator is automatically added as member."""
        deltas = project(sample_group_event)
        
        # Second delta should be membership
        member_delta = deltas[1]
        assert member_delta['table'] == 'group_members'
        assert member_delta['data']['user_id'] == sample_group_event['event_plaintext']['creator_id']
        assert member_delta['data']['added_by'] == sample_group_event['event_plaintext']['creator_id']
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_group_permissions_serialized(self, sample_group_event):
        """Test that permissions are JSON serialized."""
        deltas = project(sample_group_event)
        
        permissions_str = deltas[0]['data']['permissions']
        
        # Should be a JSON string
        assert isinstance(permissions_str, str)
        
        # Should be valid JSON
        parsed = json.loads(permissions_str)
        assert parsed == sample_group_event['event_plaintext']['permissions']
    
    @pytest.mark.unit
    @pytest.mark.event_type  
    def test_project_group_delta_structure(self, sample_group_event):
        """Test the structure of deltas returned."""
        deltas = project(sample_group_event)
        
        for delta in deltas:
            # Check delta has required fields
            assert 'op' in delta
            assert 'table' in delta
            assert 'data' in delta
            
            # Check operation type
            assert delta['op'] == 'insert'
            
            # Data should be a dict
            assert isinstance(delta['data'], dict)
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_multiple_groups(self):
        """Test projecting multiple different groups."""
        # First group
        envelope1 = {
            'event_plaintext': {
                'type': 'group',
                'group_id': 'group-1',
                'name': 'Engineering',
                'network_id': 'network-1',
                'creator_id': 'creator-1',
                'created_at': 1000,
                'permissions': {'message': ['all']}
            },
            'event_type': 'group',
            'event_id': 'group-1',
            'peer_id': 'creator-1',
            'network_id': 'network-1'
        }
        
        # Second group
        envelope2 = {
            'event_plaintext': {
                'type': 'group',
                'group_id': 'group-2',
                'name': 'Marketing',
                'network_id': 'network-1',
                'creator_id': 'creator-2',
                'created_at': 2000,
                'permissions': {'invite': ['admin']}
            },
            'event_type': 'group',
            'event_id': 'group-2',
            'peer_id': 'creator-2',
            'network_id': 'network-1'
        }
        
        deltas1 = project(envelope1)
        deltas2 = project(envelope2)
        
        # Each should produce two deltas
        assert len(deltas1) == 2
        assert len(deltas2) == 2
        
        # Different group IDs
        assert deltas1[0]['data']['group_id'] != deltas2[0]['data']['group_id']
        
        # Different creators
        assert deltas1[0]['data']['creator_id'] != deltas2[0]['data']['creator_id']
        
        # Different permissions
        perms1 = json.loads(deltas1[0]['data']['permissions'])
        perms2 = json.loads(deltas2[0]['data']['permissions'])
        assert perms1 != perms2
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_group_preserves_timestamps(self, sample_group_event):
        """Test that timestamps are preserved correctly."""
        created_at = sample_group_event['event_plaintext']['created_at']
        
        deltas = project(sample_group_event)
        
        # Group creation time
        assert deltas[0]['data']['created_at'] == created_at
        
        # Member addition time
        assert deltas[1]['data']['added_at'] == created_at