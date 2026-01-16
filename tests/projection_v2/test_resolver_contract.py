"""Tests for v2 resolver contract: block/reject/missing signer_type cases.

These tests verify the resolver contract defined in proj-v2-split-plan.md:
- ok: ProjectionContext is fully populated
- block: required deps missing (return missing list)
- reject: invalid signature or invalid data; no writes

The resolver is not yet implemented, so these tests:
1. Document expected behavior with mock results
2. Will be updated to call real resolver once implemented
"""
import pytest
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db

from tests.projection_v2.helpers import (
    build_signed_event,
    store_and_record_event,
    check_valid_event,
    check_blocked_event,
    get_blocked_deps,
    MockResolveResult,
    mock_resolve_missing_deps,
    mock_resolve_invalid_signature,
    mock_resolve_missing_signer_type,
)


class TestResolverBlockOnMissingDeps:
    """Resolver should return block when required dependencies are missing."""

    def test_block_on_missing_signed_by(self, v2_network_state):
        """Event referencing non-existent signed_by should block."""
        state = v2_network_state
        db = state['db']
        peer_id = state['peer_id']

        # Create an event with signed_by pointing to non-existent peer_shared
        fake_signer_id = 'non_existent_peer_shared_id_12345'
        private_key, _ = crypto.generate_keypair()

        blob, event_data = build_signed_event(
            event_type='admin',
            event_data={
                'user_id': 'some_user_id',
                'network_id': state['network_id'],
                'signed_by': fake_signer_id,
                'created_at': 2000,
            },
            private_key=private_key,
            signer_type='peer_shared',
        )

        # Store the event
        event_id = store_and_record_event(blob, peer_id, 2000, db)
        db.commit()

        # The event should NOT be valid (blocked on missing dep)
        assert not check_valid_event(event_id, peer_id, db), \
            "Event with missing signed_by dep should not be valid"

        # For now, verify the expected mock behavior
        result = mock_resolve_missing_deps([fake_signer_id])
        assert result.status == 'block'
        assert fake_signer_id in result.missing

    def test_block_preserves_missing_list(self, v2_network_state):
        """Block result should include all missing dependency IDs."""
        missing = ['dep_a', 'dep_b', 'dep_c']
        result = mock_resolve_missing_deps(missing)

        assert result.status == 'block'
        assert len(result.missing) == 3
        assert 'dep_a' in result.missing
        assert 'dep_b' in result.missing
        assert 'dep_c' in result.missing


class TestResolverRejectOnInvalidSignature:
    """Resolver should return reject when signature verification fails."""

    def test_reject_invalid_signature(self, v2_network_state):
        """Event with wrong signature should be rejected."""
        state = v2_network_state
        db = state['db']
        peer_id = state['peer_id']

        # Create event data and sign with one key
        signing_key, _ = crypto.generate_keypair()
        wrong_key, _ = crypto.generate_keypair()

        event_data = {
            'type': 'network',
            'network_pubkey': crypto.b64encode(crypto.generate_keypair()[1]),
            'created_at': 2000,
        }

        # Sign with signing_key but the event claims network_pubkey from wrong_key
        signed_event = crypto.sign_event(event_data, wrong_key)

        # Verify the signature fails with the actual network_pubkey
        actual_pubkey = crypto.b64decode(event_data['network_pubkey'])
        assert not crypto.verify_event(signed_event, actual_pubkey), \
            "Signature should not verify with wrong key"

        # Mock resolver should reject
        result = mock_resolve_invalid_signature()
        assert result.status == 'reject'
        assert result.error == 'invalid signature'
        assert result.ctx is None

    def test_reject_missing_signature(self, v2_network_state):
        """Event without signature field should be rejected."""
        # Event data without calling sign_event
        event_data = {
            'type': 'network',
            'network_pubkey': crypto.b64encode(crypto.generate_keypair()[1]),
            'created_at': 2000,
        }

        # Verify signature check returns False for missing signature
        pubkey = crypto.b64decode(event_data['network_pubkey'])
        assert not crypto.verify_event(event_data, pubkey), \
            "Missing signature should fail verification"


class TestResolverRejectOnMissingSignerType:
    """Resolver should return reject when signer_type is missing.

    Per the resolver contract in proj-v2-split-plan.md:
    - SignerSpec.type_field is required for signed events; missing -> reject
    - If event_data lacks the signer type field -> reject
    """

    def test_reject_missing_signer_type_field(self, v2_network_state):
        """Event without signer_type should be rejected by v2 resolver."""
        state = v2_network_state
        db = state['db']
        peer_id = state['peer_id']

        # Create admin event WITHOUT signer_type
        # (Legacy events might not have this field)
        private_key, _ = crypto.generate_keypair()

        blob, event_data = build_signed_event(
            event_type='admin',
            event_data={
                'user_id': 'some_user_id',
                'network_id': state['network_id'],
                'signed_by': state['network_id'],  # Bootstrap: signed by network
                'created_at': 2000,
            },
            private_key=private_key,
            signer_type=None,  # Explicitly omit signer_type
        )

        # Verify signer_type is not in the event
        assert 'signer_type' not in event_data, \
            "Test event should not have signer_type"

        # Mock resolver should reject
        result = mock_resolve_missing_signer_type()
        assert result.status == 'reject'
        assert 'signer_type' in result.error
        assert result.ctx is None

    def test_accept_with_signer_type_present(self, v2_network_state):
        """Event with signer_type should pass the signer_type check."""
        private_key, _ = crypto.generate_keypair()

        blob, event_data = build_signed_event(
            event_type='admin',
            event_data={
                'user_id': 'some_user_id',
                'network_id': 'some_network_id',
                'signed_by': 'some_network_id',
                'created_at': 2000,
            },
            private_key=private_key,
            signer_type='network',  # Required field present
        )

        # Verify signer_type IS in the event
        assert event_data.get('signer_type') == 'network', \
            "Test event should have signer_type='network'"

    def test_signer_type_values(self):
        """Valid signer_type values per the spec."""
        valid_types = ['peer_shared', 'invite', 'network']

        for signer_type in valid_types:
            private_key, _ = crypto.generate_keypair()
            blob, event_data = build_signed_event(
                event_type='test',
                event_data={'created_at': 1000},
                private_key=private_key,
                signer_type=signer_type,
            )
            assert event_data.get('signer_type') == signer_type


class TestResolverOkPath:
    """Tests for successful resolution (ok status)."""

    def test_network_event_self_signed_ok(self, v2_network_state):
        """Network events are self-signed and should resolve ok."""
        state = v2_network_state

        # The network event created in fixture should be valid
        assert check_valid_event(state['network_id'], state['peer_id'], state['db']), \
            "Network event should be valid after projection"

    def test_ok_result_has_context(self):
        """Ok result should have populated ProjectionContext."""
        from core.projection_v2.types import ProjectionContext, ResolveResult

        # When resolver returns ok, ctx should be populated
        ctx = ProjectionContext(
            event_id='test_id',
            event_type='network',
            event_data={'type': 'network', 'created_at': 1000},
            recorded_by='peer_id',
            recorded_at=1000,
            deps={},
            signer=None,
        )
        result = ResolveResult(status='ok', ctx=ctx)

        assert result.status == 'ok'
        assert result.ctx is not None
        assert result.ctx.event_id == 'test_id'
        assert result.ctx.event_type == 'network'
