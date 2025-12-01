"""Tests for admin.create_pure() - plaintext signed event."""
import crypto
from projectors import compute_event_id
from projectors.admin import create_pure


class TestAdminCreatePure:
    """Test admin.create_pure() - plaintext signed event."""

    def _make_deps(self):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        return {
            'user_id': 'user_123',
            'network_id': 'net_123',
            'signed_by': 'net_123',
            'signer_private_key': private_key,
        }

    def test_plaintext_not_encrypted(self):
        """Admin events are plaintext (can parse directly)."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        # Should be parseable JSON
        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'admin'
        assert blob_data['user_id'] == 'user_123'
        assert 'signature' in blob_data  # Signed

    def test_deterministic(self):
        """Same deps produce same admin_id."""
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
        """Event type is admin."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'admin'
