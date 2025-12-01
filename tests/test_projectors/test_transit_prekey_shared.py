"""Tests for transit_prekey_shared.create_pure() - signed plaintext."""
import crypto
from projectors import compute_event_id
from projectors.transit_prekey_shared import create_pure


class TestTransitPrekeySharedCreatePure:
    """Test transit_prekey_shared.create_pure()."""

    def _make_deps(self):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        return {
            'peer_shared_id': 'ps_123',
            'private_key': private_key,
            'prekey_id': 'prekey_123',
            'public_key_b64': crypto.b64encode(public_key),
        }

    def test_publishes_public_key(self):
        """Transit prekey shared publishes public key."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'transit_prekey_shared'
        assert 'signature' in blob_data
        assert 'public_key' in blob_data

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
        """Event type is transit_prekey_shared."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'transit_prekey_shared'
