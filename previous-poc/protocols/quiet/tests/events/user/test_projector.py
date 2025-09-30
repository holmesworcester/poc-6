"""
Tests for user event type projector.
"""
import pytest
import time
from protocols.quiet.events.user.projector import project


class TestUserProjector:
    """Test user event projection."""

    @pytest.fixture
    def sample_user_event(self):
        """Create a sample user event envelope."""
        return {
            'event_id': 'test-user-event-id',
            'event_plaintext': {
                'type': 'user',
                'peer_id': 'test-peer-id',
                'network_id': 'test-network',
                'group_id': 'test-group',
                'name': 'TestUser',
                'invite_pubkey': 'test-invite-pubkey',
                'invite_signature': 'test-invite-signature',
                'created_at': int(time.time() * 1000),
                'signature': 'test-signature'
            },
            'event_type': 'user',
            'peer_id': 'test-peer-id',
            'network_id': 'test-network'
        }

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_user_creates_deltas(self, sample_user_event):
        """Test that projecting a user creates the right deltas."""
        deltas = project(sample_user_event)

        # Should create exactly two deltas: users and group_members
        assert len(deltas) == 2

        # Check users delta
        user_delta = deltas[0]
        assert user_delta['op'] == 'insert'
        assert user_delta['table'] == 'users'

        # Check all fields are included
        user_data = user_delta['data']
        event_data = sample_user_event['event_plaintext']
        assert user_data['user_id'] == sample_user_event['event_id']
        assert user_data['peer_id'] == event_data['peer_id']
        assert user_data['network_id'] == event_data['network_id']
        assert user_data['name'] == event_data['name']
        assert user_data['joined_at'] == event_data['created_at']
        assert user_data['invite_pubkey'] == event_data['invite_pubkey']

        # Check group_members delta
        group_delta = deltas[1]
        assert group_delta['op'] == 'insert'
        assert group_delta['table'] == 'group_members'

        group_data = group_delta['data']
        assert group_data['group_id'] == event_data['group_id']
        assert group_data['user_id'] == sample_user_event['event_id']
        assert group_data['added_by'] == sample_user_event['event_id']  # Self-added
        assert group_data['added_at'] == event_data['created_at']

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_user_without_invite(self):
        """Test projecting user without invite fields (regular user event)."""
        envelope = {
            'event_id': 'regular-user-event',
            'event_plaintext': {
                'type': 'user',
                'peer_id': 'regular-peer',
                'network_id': 'test-network',
                'group_id': 'test-group',
                'name': 'RegularUser',
                'invite_pubkey': '',  # Empty invite
                'created_at': int(time.time() * 1000),
                'signature': 'test-signature'
            }
        }

        deltas = project(envelope)

        # Should still create deltas even without invite
        assert len(deltas) == 2
        assert deltas[0]['data']['invite_pubkey'] == ''

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_user_timestamps(self, sample_user_event):
        """Test that timestamps are preserved correctly."""
        created_at = sample_user_event['event_plaintext']['created_at']

        deltas = project(sample_user_event)

        # joined_at should match created_at
        assert deltas[0]['data']['joined_at'] == created_at
        assert deltas[1]['data']['added_at'] == created_at

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_user_delta_structure(self, sample_user_event):
        """Test the structure of the deltas returned."""
        deltas = project(sample_user_event)

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
    def test_project_multiple_users(self):
        """Test projecting multiple different users."""
        # First user
        envelope1 = {
            'event_id': 'user-event-1',
            'event_plaintext': {
                'type': 'user',
                'peer_id': 'peer-1',
                'network_id': 'network-1',
                'group_id': 'group-1',
                'name': 'User1',
                'invite_pubkey': 'invite-1',
                'created_at': 1000,
                'signature': 'sig-1'
            }
        }

        # Second user
        envelope2 = {
            'event_id': 'user-event-2',
            'event_plaintext': {
                'type': 'user',
                'peer_id': 'peer-2',
                'network_id': 'network-1',
                'group_id': 'group-1',
                'name': 'User2',
                'invite_pubkey': 'invite-2',
                'created_at': 2000,
                'signature': 'sig-2'
            }
        }

        deltas1 = project(envelope1)
        deltas2 = project(envelope2)

        # Each should produce two deltas
        assert len(deltas1) == 2
        assert len(deltas2) == 2

        # Different user IDs
        assert deltas1[0]['data']['user_id'] != deltas2[0]['data']['user_id']

        # Different peer IDs
        assert deltas1[0]['data']['peer_id'] != deltas2[0]['data']['peer_id']

        # Same network and group
        assert deltas1[0]['data']['network_id'] == deltas2[0]['data']['network_id']
        assert deltas1[1]['data']['group_id'] == deltas2[1]['data']['group_id']

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_user_with_added_by(self):
        """Test projecting user with explicit added_by field."""
        envelope = {
            'event_id': 'invited-user-event',
            'event_plaintext': {
                'type': 'user',
                'peer_id': 'invited-peer',
                'network_id': 'test-network',
                'group_id': 'test-group',
                'name': 'InvitedUser',
                'invite_pubkey': 'invite-key',
                'added_by': 'inviter-user-id',  # Explicitly set
                'created_at': int(time.time() * 1000),
                'signature': 'test-signature'
            }
        }

        deltas = project(envelope)

        # added_by should use the provided value
        assert deltas[1]['data']['added_by'] == 'inviter-user-id'

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_project_user_no_extra_fields(self, sample_user_event):
        """Test that only expected fields are included in projection."""
        deltas = project(sample_user_event)

        user_data = deltas[0]['data']
        group_data = deltas[1]['data']

        # Check only expected fields are present in users table
        expected_user_fields = {
            'user_id', 'peer_id', 'network_id',
            'name', 'joined_at', 'invite_pubkey'
        }
        assert set(user_data.keys()) == expected_user_fields

        # Check only expected fields are present in group_members table
        expected_group_fields = {
            'group_id', 'user_id', 'added_by', 'added_at'
        }
        assert set(group_data.keys()) == expected_group_fields

        # Signature should not be included in projection
        assert 'signature' not in user_data
        assert 'created_at' not in user_data  # Should be joined_at instead