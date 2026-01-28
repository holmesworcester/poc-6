"""Tests for v2 resolver contract: ok/block/reject behavior.

These tests exercise the resolver contract defined in proj-v2-split-plan.md.
"""
from core import crypto
from core.projection_v2 import resolver as v2_resolver


class TestResolverBlockOnMissingDeps:
    """Resolver should return block when required dependencies are missing."""

    def test_block_on_missing_required(self, v2_db, register_event):
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

        result = v2_resolver.resolve_event(
            ref_id='evt_missing',
            event_type=event_type,
            event_data=event_data,
            recorded_by='peer1',
            recorded_at=1000,
            db=v2_db,
        )

        assert result.status == 'block'
        assert 'missing_network' in result.missing


class TestResolverRejectOnInvalidSignature:
    """Resolver should reject when signature verification fails."""

    def test_reject_invalid_signature(self, v2_db, register_event):
        event_type = 'test_bad_sig'
        event_spec = {
            'requires': {},
            'signer': {
                'id_field': 'network_pubkey',
                'type_field': 'signer_type',
            },
        }
        register_event(event_type, event_spec)

        _, public_key = crypto.generate_keypair()
        signed_bytes = b"test-wire"
        event_data = {
            'type': event_type,
            'signer_type': 'network',
            'network_pubkey': crypto.b64encode(public_key),
            '_wire_signed_bytes': signed_bytes,
            '_wire_signature': b'not-a-real-signature',
        }

        result = v2_resolver.resolve_event(
            ref_id='evt_bad_sig',
            event_type=event_type,
            event_data=event_data,
            recorded_by='peer1',
            recorded_at=1000,
            db=v2_db,
        )

        assert result.status == 'reject'
        assert result.error and 'invalid signature' in result.error


class TestResolverRejectOnMissingSignerType:
    """Resolver should reject when signer_type is missing."""

    def test_reject_missing_signer_type(self, v2_db, register_event):
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

        result = v2_resolver.resolve_event(
            ref_id='evt_missing_signer_type',
            event_type=event_type,
            event_data=event_data,
            recorded_by='peer1',
            recorded_at=1000,
            db=v2_db,
        )

        assert result.status == 'reject'
        assert result.error and 'signer_type' in result.error


class TestResolverAcceptsEvent:
    """Resolver should return ok when deps and signer checks pass."""

    def test_accept_ok(self, v2_db, register_event):
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

        result = v2_resolver.resolve_event(
            ref_id='evt_ok',
            event_type=event_type,
            event_data=event_data,
            recorded_by='peer1',
            recorded_at=1000,
            db=v2_db,
        )

        assert result.status == 'ok'
        assert result.ctx is not None
        assert result.ctx.event_type == event_type
        assert result.ctx.deps == {}
