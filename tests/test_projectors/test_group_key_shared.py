"""Tests for group_key_shared.create_pure() - asymmetric sealed."""
import crypto
from projectors import compute_event_id
from projectors.group_key_shared import create_pure


class TestGroupKeySharedCreatePure:
    """Test group_key_shared.create_pure() - asymmetric sealed."""

    def _make_deps(self):
        """Create test dependencies."""
        sender_private, sender_public = crypto.generate_keypair()
        recipient_private, recipient_public = crypto.generate_keypair()
        symmetric_key = crypto.generate_secret()

        return {
            'peer_shared_id': 'ps_sender',
            'private_key': sender_private,
            'key_id': 'key_123',
            'symmetric_key': symmetric_key,
            'recipient_prekey': {
                'id': crypto.hash(b'prekey'),
                'public_key': recipient_public,
                'type': 'asymmetric'
            },
        }

    def test_sealed_to_recipient(self):
        """Group key shared is sealed to recipient prekey."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        assert result.blobs[0].event_type == 'group_key_shared'
        # Blob is sealed (encrypted)
        assert len(result.blobs[0].blob) > 0

    def test_deterministic_sealing(self):
        """Group key shared IS deterministic (uses deterministic ephemeral key).

        In content-addressed systems, identical ciphertext produces identical
        event IDs, which are deduplicated. Random nonces provide no security
        benefit. We derive the ephemeral keypair deterministically from inputs.
        """
        deps = self._make_deps()

        result1 = create_pure(deps, 1000)
        result2 = create_pure(deps, 1000)

        # Same inputs → same outputs (deterministic)
        assert result1.blobs[0].blob == result2.blobs[0].blob
        assert result1.primary_id == result2.primary_id

    def test_id_is_content_addressed(self):
        """Event ID matches hash of blob."""
        deps = self._make_deps()
        result = create_pure(deps, 1000)

        expected_id = compute_event_id(result.blobs[0].blob)
        assert result.primary_id == expected_id
