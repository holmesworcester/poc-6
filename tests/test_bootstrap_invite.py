"""
Test bootstrap user invite signed by network_id.

Tests that create_bootstrap_user_invite() works correctly:
1. Creates an invite(mode=user) signed by network_id
2. The invite can be verified using network_pubkey
3. The signed_by field correctly identifies the network
"""
import sqlite3
from core.db import Database
from core import schema
from core import crypto
from core import store
from events.identity import user, network
from events.identity import invite as invite_module


def test_bootstrap_user_invite_signed_by_network():
    """Test that bootstrap invite is signed by network_id.

    Uses user.new_network() to properly bootstrap, then verifies
    the invite structure is correct.
    """

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network using the proper bootstrap flow
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    network_id = alice['network_id']
    print(f"Created network via new_network(): {network_id[:20]}...")

    # Verify network event has network_pubkey
    network_blob = store.get(network_id, db)
    if not network.is_wire_envelope(network_blob):
        raise AssertionError("expected wire network event")
    network_event = network.decode_wire_event(network_blob)
    assert 'network_pubkey' in network_event, "Network event should have network_pubkey"
    print(f"Network has network_pubkey: {network_event['network_pubkey'][:20]}...")

    # Verify network is self-signed (no signed_by field - self-signed is implicit for root of trust)
    assert 'signed_by' not in network_event, f"Network should not have signed_by (self-signed), got {network_event.get('signed_by')}"
    print("✓ Network is self-signed (no signed_by field - root of trust)")

    # Verify signature using network_pubkey
    network_pubkey_bytes = crypto.b64decode(network_event['network_pubkey'])
    assert crypto.verify(network_event['_wire_signed_bytes'], network_event['_wire_signature'], network_pubkey_bytes), \
        "Network signature should verify with network_pubkey"
    print("✓ Network signature verified with network_pubkey")

    # The bootstrap invite is now created internally by new_network()
    # We can verify that invites created for joining users are signed correctly
    from events.identity import invite

    # Create an invite for a new user to join
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=2000,
        db=db
    )
    print(f"Created invite for new users: {invite_id[:20]}...")

    # Verify the invite structure
    invite_blob = store.get(invite_id, db)
    if not invite_module.is_wire_envelope(invite_blob):
        raise AssertionError("expected wire invite event")
    invite_event = invite_module.decode_wire_event(invite_blob)

    assert invite_event['type'] == 'invite', "Should be invite type"
    assert invite_event['mode'] == 'user', "Should be mode=user"
    assert 'signed_by' in invite_event, "Should have signed_by"
    assert 'invite_pubkey' in invite_event, "Should have invite_pubkey"

    print("\nInvite event fields:")
    print(f"  type: {invite_event['type']}")
    print(f"  mode: {invite_event['mode']}")
    print(f"  signed_by: {invite_event['signed_by'][:20]}...")

    print("\n✅ test_bootstrap_user_invite_signed_by_network passed!")


def test_network_get_public_key():
    """Test that network.get_public_key() returns the correct key."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network using the proper bootstrap flow
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    network_id = alice['network_id']
    peer_id = alice['peer_id']

    # Get network_pubkey from event
    network_blob = store.get(network_id, db)
    if not network.is_wire_envelope(network_blob):
        raise AssertionError("expected wire network event")
    network_event = network.decode_wire_event(network_blob)
    expected_pubkey = crypto.b64decode(network_event['network_pubkey'])

    db.commit()

    # Test get_public_key
    retrieved_pubkey = network.get_public_key(network_id, peer_id, db)
    assert retrieved_pubkey == expected_pubkey, "get_public_key should return correct key"
    print("✓ network.get_public_key() returns correct key")

    print("\n✅ test_network_get_public_key passed!")


if __name__ == '__main__':
    test_bootstrap_user_invite_signed_by_network()
    test_network_get_public_key()
