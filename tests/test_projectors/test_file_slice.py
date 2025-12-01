"""Tests for file_slice.create_pure() - unsigned, unencrypted."""
import crypto
from projectors import compute_event_id
from projectors.file_slice import create_pure


class TestFileSliceCreatePure:
    """Test file_slice.create_pure() - unsigned, unencrypted."""

    def _make_deps(self):
        """Create test dependencies."""
        return {
            'file_id': 'file_123',
            'slice_number': 0,
            'nonce': b'\x00' * 24,
            'ciphertext': b'encrypted_content',
            'poly_tag': b'\x00' * 16,
            'signed_by': 'ps_123',
        }

    def test_no_signature(self):
        """File slices are not signed (integrity via root_hash)."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'file_slice'
        assert 'signature' not in blob_data  # Not signed

    def test_deterministic(self):
        """Same deps produce same slice ID."""
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
        """Event type is file_slice."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'file_slice'

    def test_different_slice_number_different_id(self):
        """Different slice numbers produce different IDs."""
        deps = self._make_deps()

        result1 = create_pure(deps, 1000)
        deps['slice_number'] = 1
        result2 = create_pure(deps, 1000)

        assert result1.primary_id != result2.primary_id
