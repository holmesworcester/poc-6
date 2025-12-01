"""Tests for channel.create_pure() - encrypted event."""
import crypto
from projectors import compute_event_id, CreateResult
from projectors.channel import create_pure


class TestChannelCreatePure:
    """Test channel.create_pure() - encrypted event."""

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
        }

    def test_encrypted_blob(self):
        """Channel events are encrypted."""
        deps = self._make_deps()
        result = create_pure(deps, "general", "grp_123", 1000)

        # Encrypted blob has content beyond just the key hint
        blob = result.blobs[0].blob
        assert len(blob) > 32

    def test_returns_create_result(self):
        """Returns CreateResult with correct structure."""
        deps = self._make_deps()
        result = create_pure(deps, "general", "grp_123", 1000)

        assert isinstance(result, CreateResult)
        assert len(result.blobs) == 1
        assert result.blobs[0].event_type == 'channel'

    def test_deterministic(self):
        """Same deps produce same channel_id."""
        deps = self._make_deps()

        result1 = create_pure(deps, "general", "grp_123", 1000)
        result2 = create_pure(deps, "general", "grp_123", 1000)

        assert result1.primary_id == result2.primary_id

    def test_id_is_content_addressed(self):
        """Event ID matches hash of blob."""
        deps = self._make_deps()
        result = create_pure(deps, "general", "grp_123", 1000)

        expected_id = compute_event_id(result.blobs[0].blob)
        assert result.primary_id == expected_id

    def test_different_name_different_id(self):
        """Different channel name produces different ID."""
        deps = self._make_deps()

        result1 = create_pure(deps, "general", "grp_123", 1000)
        result2 = create_pure(deps, "random", "grp_123", 1000)

        assert result1.primary_id != result2.primary_id
