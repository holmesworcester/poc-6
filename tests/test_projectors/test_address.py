"""Tests for address.create_pure() - signed plaintext."""
import crypto
from projectors import compute_event_id
from projectors.address import create_pure


class TestAddressCreatePure:
    """Test address.create_pure() - signed plaintext."""

    def _make_deps(self):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        return {
            'peer_shared_id': 'ps_123',
            'private_key': private_key,
        }

    def test_signed_with_peer_key(self):
        """Address is signed plaintext."""
        deps = self._make_deps()
        result = create_pure(deps, '127.0.0.1', 6100, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'address'
        assert blob_data['ip'] == '127.0.0.1'
        assert blob_data['port'] == 6100
        assert 'signature' in blob_data

    def test_deterministic(self):
        """Same deps produce same address ID."""
        deps = self._make_deps()

        result1 = create_pure(deps, '127.0.0.1', 6100, 1000)
        result2 = create_pure(deps, '127.0.0.1', 6100, 1000)

        assert result1.primary_id == result2.primary_id

    def test_id_is_content_addressed(self):
        """Event ID matches hash of blob."""
        deps = self._make_deps()
        result = create_pure(deps, '127.0.0.1', 6100, 1000)

        expected_id = compute_event_id(result.blobs[0].blob)
        assert result.primary_id == expected_id

    def test_event_type_correct(self):
        """Event type is address."""
        deps = self._make_deps()
        result = create_pure(deps, '127.0.0.1', 6100, 1000)

        assert result.blobs[0].event_type == 'address'

    def test_different_ip_different_id(self):
        """Different IP produces different ID."""
        deps = self._make_deps()

        result1 = create_pure(deps, '127.0.0.1', 6100, 1000)
        result2 = create_pure(deps, '192.168.1.1', 6100, 1000)

        assert result1.primary_id != result2.primary_id
