#!/usr/bin/env python3
"""Test that signature verification fix works correctly."""

import sys
import sqlite3
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from protocols.quiet.handlers.signature import verify_signature
from core.crypto import generate_keypair, sign

def test_peer_event_self_attestation():
    """Test that peer events can still use self-attested public keys."""
    # Generate a keypair
    private_key, public_key = generate_keypair()

    # Create a peer event with its own public key
    event = {
        'type': 'peer',
        'public_key': public_key.hex(),
        'peer_secret_id': 'test_local_id',
        'created_at': 1234567890
    }

    # Sign it
    canonical = json.dumps(event, sort_keys=True).encode('utf-8')
    signature = sign(canonical, private_key)
    event['signature'] = signature.hex()

    # Create envelope
    envelope = {
        'event_plaintext': event,
        'event_type': 'peer'
    }

    # Verify - should succeed for peer events
    result = verify_signature(envelope)

    assert result.get('sig_checked') == True
    assert result.get('sig_failed') != True
    print("✓ Peer event self-attestation works")
    return True

def test_non_peer_event_requires_dependency():
    """Test that non-peer events fail without resolved peer dependency."""
    # Generate a keypair
    private_key, public_key = generate_keypair()

    # Create a message event with self-attested public key
    event = {
        'type': 'message',
        'peer_id': 'some_peer_id',
        'public_key': public_key.hex(),  # Self-attested - should NOT work
        'content': 'Hello',
        'created_at': 1234567890
    }

    # Sign it
    canonical = json.dumps(event, sort_keys=True).encode('utf-8')
    signature = sign(canonical, private_key)
    event['signature'] = signature.hex()

    # Create envelope WITHOUT resolved dependencies
    envelope = {
        'event_plaintext': event,
        'event_type': 'message'
    }

    # Verify - should FAIL for non-peer events without dependencies
    result = verify_signature(envelope)

    assert result.get('sig_checked') == True
    assert result.get('sig_failed') == True
    assert 'peer dependency not resolved' in result.get('error', '')
    print("✓ Non-peer event correctly fails without resolved dependency")
    return True

def test_non_peer_event_with_dependency():
    """Test that non-peer events work with resolved peer dependency."""
    # Generate a keypair
    private_key, public_key = generate_keypair()

    # Create a message event
    event = {
        'type': 'message',
        'peer_id': 'peer_event_id_123',
        'content': 'Hello',
        'created_at': 1234567890
    }

    # Sign it
    canonical = json.dumps(event, sort_keys=True).encode('utf-8')
    signature = sign(canonical, private_key)
    event['signature'] = signature.hex()

    # Create envelope WITH resolved peer dependency
    envelope = {
        'event_plaintext': event,
        'event_type': 'message',
        'resolved_deps': {
            'peer:peer_event_id_123': {
                'event_plaintext': {
                    'type': 'peer',
                    'public_key': public_key.hex()  # Public key from peer dependency
                }
            }
        }
    }

    # Verify - should succeed with proper dependency
    result = verify_signature(envelope)

    assert result.get('sig_checked') == True
    assert result.get('sig_failed') != True
    print("✓ Non-peer event works with resolved dependency")
    return True

if __name__ == '__main__':
    print("Testing signature verification fix...\n")

    success = True
    success = test_peer_event_self_attestation() and success
    success = test_non_peer_event_requires_dependency() and success
    success = test_non_peer_event_with_dependency() and success

    if success:
        print("\n✅ All tests passed! The fix is working correctly.")
        print("\nThe vulnerability has been fixed:")
        print("- Peer events can still self-attest (required for bootstrapping)")
        print("- Non-peer events CANNOT use self-attested keys (prevents impersonation)")
        print("- Non-peer events require resolved peer dependencies for verification")
    else:
        print("\n❌ Some tests failed")
        sys.exit(1)
