"""Tests for group_prekey_shared.create_pure() - signed plaintext with context."""
import crypto
from projectors import compute_event_id
from projectors.group_prekey_shared import create_pure


class TestGroupPrekeySharedCreatePure:
    """Test group_prekey_shared.create_pure()."""

    def _make_deps(self, with_group=True, with_user=False):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        deps = {
            'peer_shared_id': 'ps_123',
            'private_key': private_key,
            'prekey_id': 'prekey_123',
            'public_key_b64': crypto.b64encode(public_key),
        }
        if with_group:
            deps['group_id'] = 'grp_123'
            deps['key_id'] = 'key_123'
        if with_user:
            deps['user_id'] = 'user_123'
        return deps

    def test_with_group_context(self):
        """Group prekey shared with group context."""
        deps = self._make_deps(with_group=True)
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'group_prekey_shared'
        assert blob_data['group_id'] == 'grp_123'
        assert blob_data['key_id'] == 'key_123'

    def test_with_user_context(self):
        """Group prekey shared with user context (link invite)."""
        deps = self._make_deps(with_group=False, with_user=True)
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'group_prekey_shared'
        assert blob_data['user_id'] == 'user_123'
        assert 'group_id' not in blob_data

    def test_signed(self):
        """Group prekey shared is signed."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert 'signature' in blob_data

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
        """Event type is group_prekey_shared."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'group_prekey_shared'
