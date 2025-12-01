"""Tests for network_joined.create_pure() - signed plaintext."""
import crypto
from projectors import compute_event_id
from projectors.network_joined import create_pure


class TestNetworkJoinedCreatePure:
    """Test network_joined.create_pure() - signed plaintext."""

    def _make_deps(self):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        return {
            'peer_id': 'peer_123',
            'peer_shared_id': 'ps_123',
            'private_key': private_key,
            'inviter_peer_shared_id': 'ps_inviter',
        }

    def test_signed_event(self):
        """Network joined is signed."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'network_joined'
        assert 'signature' in blob_data

    def test_contains_inviter_info(self):
        """Network joined contains inviter peer_shared_id."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['inviter_peer_shared_id'] == 'ps_inviter'

    def test_deterministic(self):
        """Same deps produce same event ID."""
        deps = self._make_deps()

        result1 = create_pure(deps, 1000)
        result2 = create_pure(deps, 1000)

        assert result1.primary_id == result2.primary_id

    def test_id_is_content_addressed(self):
        """Event ID matches hash of blob."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        expected_id = compute_event_id(result.blobs[0].blob)
        assert result.primary_id == expected_id

    def test_event_type_correct(self):
        """Event type is network_joined."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'network_joined'
