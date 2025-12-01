"""Tests for group.create_pure() with chained creation."""
import crypto
from projectors import compute_event_id, CreateResult
from projectors.group import create_pure


class TestGroupCreatePure:
    """Test group.create_pure() with chained creation."""

    def _make_deps(self):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        key_material = crypto.generate_secret()
        return {
            'peer_shared_id': 'ps_123',
            'private_key': private_key,
            'key_material': key_material,
        }

    def test_creates_two_blobs(self):
        """Group creates group_key blob first, then group blob."""
        deps = self._make_deps()
        result = create_pure(deps, "Test Group", 1000)

        assert len(result.blobs) == 2
        assert result.blobs[0].event_type == 'group_key'
        assert result.blobs[1].event_type == 'group'

    def test_group_references_key_id(self):
        """Group blob contains computed key_id."""
        deps = self._make_deps()
        result = create_pure(deps, "Test Group", 1000)

        key_id = result.blobs[0].event_id
        group_id = result.blobs[1].event_id

        # Primary is group, not key
        assert result.primary_id == group_id

        # Key blob is plaintext JSON
        key_data = crypto.parse_json(result.blobs[0].blob)
        assert key_data['type'] == 'group_key'

        # Verify key_id matches
        computed_key_id = compute_event_id(result.blobs[0].blob)
        assert computed_key_id == key_id

    def test_deterministic_key_id(self):
        """Same key material produces same key_id."""
        deps = self._make_deps()

        result1 = create_pure(deps, "Test Group", 1000)
        result2 = create_pure(deps, "Test Group", 1000)

        # Key IDs match (deterministic)
        assert result1.blobs[0].event_id == result2.blobs[0].event_id

    def test_main_group_flag(self):
        """Can create main group with is_main flag."""
        deps = self._make_deps()
        result = create_pure(deps, "Main", 1000, is_main=True)

        # Still produces valid result
        assert len(result.blobs) == 2
        assert result.primary_id != ""

    def test_with_network_id(self):
        """Can specify network_id for group."""
        deps = self._make_deps()
        result = create_pure(deps, "Test", 1000, network_id="net_123")

        assert len(result.blobs) == 2
        assert result.primary_id != ""
