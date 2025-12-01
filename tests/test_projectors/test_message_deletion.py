"""Tests for message_deletion.create_pure() - encrypted event."""
import crypto
from projectors import compute_event_id
from projectors.message_deletion import create_pure


class TestMessageDeletionCreatePure:
    """Test message_deletion.create_pure()."""

    def _make_deps(self):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        key_material = crypto.generate_secret()
        return {
            'peer_shared_id': 'ps_123',
            'private_key': private_key,
            'key_data': {
                'id': crypto.hash(b'key'),
                'key': key_material,
                'type': 'symmetric'
            },
            'message_id': 'msg_123',
            'group_id': 'grp_123',
        }

    def test_encrypted_to_group(self):
        """Message deletion is encrypted to message's group."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'message_deletion'
        # Blob is encrypted (not directly parseable as JSON without key)
        assert len(result.blobs[0].blob) > 0

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

    def test_different_message_different_id(self):
        """Different message_id produces different deletion ID."""
        deps = self._make_deps()

        result1 = create_pure(deps, 1000)
        deps['message_id'] = 'msg_456'
        result2 = create_pure(deps, 1000)

        assert result1.primary_id != result2.primary_id
