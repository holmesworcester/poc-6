"""Tests for peer.create_pure() - local-only keypair."""
import crypto
from projectors import compute_event_id
from projectors.peer import create_pure


class TestPeerCreatePure:
    """Test peer.create_pure() - local-only keypair."""

    def _make_deps(self):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        return {'private_key': private_key, 'public_key': public_key}

    def test_contains_both_keys(self):
        """Peer blob contains both public and private keys."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'peer'
        assert 'public_key' in blob_data
        assert 'private_key' in blob_data

    def test_deterministic(self):
        """Same keypair produces same peer_id."""
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
        """Event type is peer."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'peer'
