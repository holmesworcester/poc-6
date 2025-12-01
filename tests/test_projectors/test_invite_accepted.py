"""Tests for invite_accepted.create_pure() - local-only."""
import crypto
from projectors import compute_event_id
from projectors.invite_accepted import create_pure


class TestInviteAcceptedCreatePure:
    """Test invite_accepted.create_pure() - local-only."""

    def _make_deps(self):
        """Create test dependencies."""
        private_key, public_key = crypto.generate_keypair()
        return {
            'invite_id': 'inv_123',
            'invite_prekey_id': 'prekey_123',
            'invite_private_key': private_key,
            'peer_id': 'peer_123',
        }

    def test_stores_invite_data(self):
        """Invite accepted stores all invite link data."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert blob_data['type'] == 'invite_accepted'
        assert blob_data['invite_id'] == 'inv_123'
        assert blob_data['invite_prekey_id'] == 'prekey_123'

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
        """Event type is invite_accepted."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'invite_accepted'

    def test_not_signed(self):
        """Invite accepted is local-only, not signed."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert 'signature' not in blob_data  # Local-only, no signing
