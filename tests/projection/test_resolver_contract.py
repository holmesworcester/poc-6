"""Tests for v2 resolver contract: ok/block/reject behavior.

These tests exercise the resolver contract defined in proj-v2-split-plan.md.
"""
from core import crypto
from core.projection import resolver as resolver


class TestResolverBlockOnMissingDeps:
    """Resolver should return block when required dependencies are missing."""

    def test_block_on_missing_required(self, projection_db, register_event):
        event_type = 'test_missing_dep'
        event_spec = {
            'requires': {
                'network': {
                    'source': 'table',
                    'table': 'networks',
                    'key': 'network_id',
                },
            },
        }
        register_event(event_type, event_spec)

        event_data = {
            'type': event_type,
            'network_id': 'missing_network',
        }

        result = resolver.resolve_event(
            ref_id='evt_missing',
            event_type=event_type,
            event_data=event_data,
            recorded_by='peer1',
            recorded_at=1000,
            db=projection_db,
        )

        assert result.status == 'block'
        assert 'missing_network' in result.missing


class TestResolverRejectOnInvalidSignature:
    """Resolver should reject when signature verification fails."""

    def _make_event_data(self, event_type, public_key, signed_bytes, signature):
        return {
            'type': event_type,
            'signer_type': 'network',
            'network_pubkey': crypto.b64encode(public_key),
            '_wire_signed_bytes': signed_bytes,
            '_wire_signature': signature,
        }

    def _resolve(self, projection_db, event_type, event_data):
        return resolver.resolve_event(
            ref_id='evt_bad_sig',
            event_type=event_type,
            event_data=event_data,
            recorded_by='peer1',
            recorded_at=1000,
            db=projection_db,
        )

    def test_reject_signature_too_short(self, projection_db, register_event):
        """Signature shorter than 64 bytes should be rejected."""
        event_type = 'test_bad_sig_short'
        event_spec = {
            'requires': {},
            'signer': {'id_field': 'network_pubkey', 'type_field': 'signer_type'},
        }
        register_event(event_type, event_spec)

        _, public_key = crypto.generate_keypair()
        event_data = self._make_event_data(
            event_type, public_key, b"test-wire", b'too-short'
        )
        result = self._resolve(projection_db, event_type, event_data)

        assert result.status == 'reject'
        assert result.error and 'invalid signature' in result.error

    def test_reject_signature_too_long(self, projection_db, register_event):
        """Signature longer than 64 bytes should be rejected."""
        event_type = 'test_bad_sig_long'
        event_spec = {
            'requires': {},
            'signer': {'id_field': 'network_pubkey', 'type_field': 'signer_type'},
        }
        register_event(event_type, event_spec)

        _, public_key = crypto.generate_keypair()
        event_data = self._make_event_data(
            event_type, public_key, b"test-wire", b'x' * 65
        )
        result = self._resolve(projection_db, event_type, event_data)

        assert result.status == 'reject'
        assert result.error and 'invalid signature' in result.error

    def test_reject_signature_wrong_content(self, projection_db, register_event):
        """64-byte signature with wrong content should be rejected."""
        event_type = 'test_bad_sig_wrong'
        event_spec = {
            'requires': {},
            'signer': {'id_field': 'network_pubkey', 'type_field': 'signer_type'},
        }
        register_event(event_type, event_spec)

        _, public_key = crypto.generate_keypair()
        event_data = self._make_event_data(
            event_type, public_key, b"test-wire", b'\x00' * 64
        )
        result = self._resolve(projection_db, event_type, event_data)

        assert result.status == 'reject'
        assert result.error and 'invalid signature' in result.error

    def test_reject_signature_wrong_key(self, projection_db, register_event):
        """Valid signature but wrong public key should be rejected."""
        event_type = 'test_bad_sig_key'
        event_spec = {
            'requires': {},
            'signer': {'id_field': 'network_pubkey', 'type_field': 'signer_type'},
        }
        register_event(event_type, event_spec)

        private_key, _ = crypto.generate_keypair()
        _, wrong_public_key = crypto.generate_keypair()
        signed_bytes = b"test-wire"
        real_signature = crypto.sign(signed_bytes, private_key)

        event_data = self._make_event_data(
            event_type, wrong_public_key, signed_bytes, real_signature
        )
        result = self._resolve(projection_db, event_type, event_data)

        assert result.status == 'reject'
        assert result.error and 'invalid signature' in result.error

    def test_reject_empty_signature(self, projection_db, register_event):
        """Empty signature should be rejected."""
        event_type = 'test_bad_sig_empty'
        event_spec = {
            'requires': {},
            'signer': {'id_field': 'network_pubkey', 'type_field': 'signer_type'},
        }
        register_event(event_type, event_spec)

        _, public_key = crypto.generate_keypair()
        event_data = self._make_event_data(
            event_type, public_key, b"test-wire", b''
        )
        result = self._resolve(projection_db, event_type, event_data)

        assert result.status == 'reject'
        assert result.error and 'invalid signature' in result.error

    def test_reject_public_key_too_short(self, projection_db, register_event):
        """Public key shorter than 32 bytes should be rejected."""
        event_type = 'test_bad_pubkey_short'
        event_spec = {
            'requires': {},
            'signer': {'id_field': 'network_pubkey', 'type_field': 'signer_type'},
        }
        register_event(event_type, event_spec)

        event_data = {
            'type': event_type,
            'signer_type': 'network',
            'network_pubkey': crypto.b64encode(b'short-key'),
            '_wire_signed_bytes': b"test-wire",
            '_wire_signature': b'\x00' * 64,
        }
        result = self._resolve(projection_db, event_type, event_data)

        assert result.status == 'reject'
        assert result.error and 'invalid signature' in result.error

    def test_reject_public_key_too_long(self, projection_db, register_event):
        """Public key longer than 32 bytes should be rejected."""
        event_type = 'test_bad_pubkey_long'
        event_spec = {
            'requires': {},
            'signer': {'id_field': 'network_pubkey', 'type_field': 'signer_type'},
        }
        register_event(event_type, event_spec)

        event_data = {
            'type': event_type,
            'signer_type': 'network',
            'network_pubkey': crypto.b64encode(b'x' * 33),
            '_wire_signed_bytes': b"test-wire",
            '_wire_signature': b'\x00' * 64,
        }
        result = self._resolve(projection_db, event_type, event_data)

        assert result.status == 'reject'
        assert result.error and 'invalid signature' in result.error


class TestResolverRejectOnMissingSignerType:
    """Resolver should reject when signer_type is missing."""

    def test_reject_missing_signer_type(self, projection_db, register_event):
        event_type = 'test_missing_signer_type'
        event_spec = {
            'requires': {},
            'signer': {
                'id_field': 'network_pubkey',
                'type_field': 'signer_type',
            },
        }
        register_event(event_type, event_spec)

        _, public_key = crypto.generate_keypair()
        event_data = {
            'type': event_type,
            'network_pubkey': crypto.b64encode(public_key),
        }

        result = resolver.resolve_event(
            ref_id='evt_missing_signer_type',
            event_type=event_type,
            event_data=event_data,
            recorded_by='peer1',
            recorded_at=1000,
            db=projection_db,
        )

        assert result.status == 'reject'
        assert result.error and 'signer_type' in result.error


class TestResolverAcceptsEvent:
    """Resolver should return ok when deps and signer checks pass."""

    def test_accept_ok(self, projection_db, register_event):
        event_type = 'test_ok'
        event_spec = {
            'requires': {},
            'optional': {},
        }
        register_event(event_type, event_spec)

        event_data = {
            'type': event_type,
            'created_at': 1000,
        }

        result = resolver.resolve_event(
            ref_id='evt_ok',
            event_type=event_type,
            event_data=event_data,
            recorded_by='peer1',
            recorded_at=1000,
            db=projection_db,
        )

        assert result.status == 'ok'
        assert result.ctx is not None
        assert result.ctx.event_type == event_type
        assert result.ctx.deps == {}
