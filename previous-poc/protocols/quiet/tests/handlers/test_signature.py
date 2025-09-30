#!/usr/bin/env python3
"""Tests for signature handler."""
import sys
sys.path.insert(0, '/home/hwilson/quiet-python-poc-5')

from protocols.quiet.handlers.signature import handler as signature_handler
from core.crypto import generate_keypair, sign, verify


class TestSignature:

    def setup_method(self):
        """Set up handler."""
        self.handler = signature_handler
        # No database needed - signature handler uses resolved_deps

    def test_sign_self_created_event_with_peer_secret(self):
        """Test signing a self-created event when peer_secret is in resolved_deps."""
        # Generate keypair
        priv, pub = generate_keypair()

        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test', 'data': 'value'},
            'self_created': True,
            'deps_included_and_valid': True,
            'resolved_deps': {
                'peer_secret:test_secret': {
                    'private_key': priv.hex(),
                    'public_key': pub.hex()
                }
            }
        }

        results = list(self.handler(envelope, None))  # db not used

        assert len(results) == 1
        result = results[0]
        assert 'signature' in result['event_plaintext']
        assert result.get('sig_checked') is True
        assert result.get('self_signed') is True

    def test_sign_not_ready_without_signer(self):
        """Test that signing is skipped when no peer_secret in resolved_deps."""
        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'self_created': True,
            'deps_included_and_valid': True,
            'resolved_deps': {}  # No signer available
        }

        results = list(self.handler(envelope, None))

        # Should return empty, waiting for signer
        assert len(results) == 0

    def test_verify_external_event_signature(self):
        """Test verifying signature on external event."""
        # Generate keypair for external peer
        priv, pub = generate_keypair()
        peer_id = 'peer123'

        # Create signed envelope - sign the canonicalized event_plaintext
        import json
        event_plaintext = {'type': 'test', 'peer_id': peer_id}
        canonical = json.dumps(event_plaintext, sort_keys=True).encode()
        signature = sign(canonical, priv)

        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test', 'signature': signature.hex(), 'peer_id': peer_id},
            'self_created': False,
            'deps_included_and_valid': True,
            'resolved_deps': {
                f'peer:{peer_id}': {
                    'event_plaintext': {
                        'type': 'peer',
                        'public_key': pub.hex()
                    }
                }
            }
        }

        results = list(self.handler(envelope, None))

        assert len(results) == 1
        assert results[0].get('sig_checked') is True
        assert results[0].get('sig_failed') is not True

    def test_invalid_signature_marks_envelope(self):
        """Test that invalid signature marks envelope as invalid."""
        peer_id = 'peer123'

        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test', 'signature': 'invalid_signature', 'peer_id': peer_id},
            'self_created': False,
            'deps_included_and_valid': True,
            'resolved_deps': {
                f'peer:{peer_id}': {
                    'event_plaintext': {
                        'type': 'peer',
                        'public_key': 'abcd1234'  # Some public key
                    }
                }
            }
        }

        results = list(self.handler(envelope, None))

        assert len(results) == 1
        assert results[0].get('sig_checked') is True
        assert results[0].get('sig_failed') is True

    def test_local_only_event_skips_signature(self):
        """Test that local_only events skip signature processing."""
        envelope = {
            'event_type': 'peer_secret',
            'event_plaintext': {'type': 'peer_secret'},
            'local_only': True,
            'self_created': True
        }

        # Should not match filter
        from protocols.quiet.handlers.signature import filter_func
        assert filter_func(envelope) is False

    def test_missing_signature_on_verify(self):
        """Test handling when signature is missing on verify."""
        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},  # No signature
            'self_created': False,
            'deps_included_and_valid': True
        }

        results = list(self.handler(envelope, None))

        assert len(results) == 1
        assert results[0].get('error') == 'No signature in event'
        assert results[0].get('sig_failed') is True

    def test_peer_event_self_attested(self):
        """Test that peer events can use self-attested public keys."""
        # Generate keypair
        priv, pub = generate_keypair()

        # Sign peer event with its own key
        import json
        event_plaintext = {'type': 'peer', 'public_key': pub.hex()}
        canonical = json.dumps(event_plaintext, sort_keys=True).encode()
        signature = sign(canonical, priv)

        envelope = {
            'event_type': 'peer',
            'event_plaintext': {
                'type': 'peer',
                'public_key': pub.hex(),
                'signature': signature.hex()
            },
            'self_created': False,
            'deps_included_and_valid': True,
            'resolved_deps': {}  # No peer dep needed for peer events
        }

        results = list(self.handler(envelope, None))

        assert len(results) == 1
        assert results[0].get('sig_checked') is True
        assert results[0].get('sig_failed') is not True

    def test_filters_match_correctly(self):
        """Test that handler filters match correct envelopes."""
        from protocols.quiet.handlers.signature import filter_func

        # Should not process without deps resolved and signer
        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'self_created': True,
            'deps_included_and_valid': False
        }
        assert filter_func(envelope) is False

        # Should process when deps resolved and signer present
        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'self_created': True,
            'deps_included_and_valid': True,
            'resolved_deps': {
                'peer_secret:test': {'private_key': 'abc123'}
            }
        }
        assert filter_func(envelope) is True