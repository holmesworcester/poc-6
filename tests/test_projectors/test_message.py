"""Tests for message.create_pure()."""
import crypto
from projectors import compute_event_id, CreateResult
from projectors.message import create_pure


class TestMessageCreatePure:
    """Test message.create_pure()."""

    def _make_deps(self):
        """Create test dependencies."""
        key_material = crypto.generate_secret()
        private_key, public_key = crypto.generate_keypair()
        return {
            'channel_id': 'ch_123',
            'group_id': 'grp_123',
            'peer_shared_id': 'ps_123',
            'user_id': 'user_123',
            'private_key': private_key,
            'key_data': {
                'id': crypto.hash(b'key'),
                'key': key_material,
                'type': 'symmetric'
            },
            'disappearing_time_ms': 0,
        }

    def test_deterministic(self):
        """Same deps produce same blob and ID."""
        deps = self._make_deps()

        result1 = create_pure(deps, "Hello", 1000)
        result2 = create_pure(deps, "Hello", 1000)

        assert result1.primary_id == result2.primary_id
        assert result1.blobs[0].blob == result2.blobs[0].blob

    def test_returns_create_result(self):
        """Returns CreateResult with correct structure."""
        deps = self._make_deps()
        result = create_pure(deps, "Hello", 1000)

        assert isinstance(result, CreateResult)
        assert len(result.blobs) == 1
        assert result.blobs[0].event_type == 'message'
        assert result.primary_id == result.blobs[0].event_id

    def test_id_is_content_addressed(self):
        """Event ID matches hash of blob."""
        deps = self._make_deps()
        result = create_pure(deps, "Hello", 1000)
        expected_id = compute_event_id(result.blobs[0].blob)

        assert result.primary_id == expected_id

    def test_different_content_different_id(self):
        """Different content produces different ID."""
        deps = self._make_deps()

        result1 = create_pure(deps, "Hello", 1000)
        result2 = create_pure(deps, "World", 1000)

        assert result1.primary_id != result2.primary_id

    def test_different_timestamp_different_id(self):
        """Different timestamp produces different ID."""
        deps = self._make_deps()

        result1 = create_pure(deps, "Hello", 1000)
        result2 = create_pure(deps, "Hello", 2000)

        assert result1.primary_id != result2.primary_id
