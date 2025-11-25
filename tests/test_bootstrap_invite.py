"""
Test Phase 4: Bootstrap user invite signed by network_id.

Tests that create_bootstrap_user_invite() works correctly:
1. Creates an invite(mode=user) signed by network_id
2. The invite can be verified using network_pubkey
3. The signed_by field correctly identifies the network
"""
import sqlite3
from db import Database
import schema
import crypto
import store
from events.identity import peer, network, invite
from events.group import group, group_key
from events.content import channel


def test_bootstrap_user_invite_signed_by_network():
    """Test that bootstrap invite is signed by network_id."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create a bootstrap peer (first peer on the network)
    peer_id, peer_shared_id = peer.create(t_ms=1000, db=db)
    print(f"Created peer: {peer_id[:20]}...")

    # Create prerequisites for network
    all_users_group_id, all_users_key_id = group.create(
        name='all_users', peer_id=peer_id, peer_shared_id=peer_shared_id, t_ms=1100, db=db
    )
    admins_group_id, admins_key_id = group.create(
        name='admins', peer_id=peer_id, peer_shared_id=peer_shared_id, t_ms=1200, db=db
    )
    channel_id = channel.create(
        name='main', peer_id=peer_id, peer_shared_id=peer_shared_id, t_ms=1300, db=db,
        group_id=all_users_group_id, key_id=all_users_key_id, is_main=True
    )

    # Create network (now returns network_private_key)
    network_id, network_private_key = network.create(
        all_users_group_id=all_users_group_id,
        admins_group_id=admins_group_id,
        creator_user_id='',  # Empty for bootstrap
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=1400,
        db=db
    )
    print(f"Created network: {network_id[:20]}...")
    print(f"Got network_private_key: {len(network_private_key)} bytes")

    # Verify network event has network_pubkey
    network_blob = store.get(network_id, db)
    network_event = crypto.parse_json(network_blob)
    assert 'network_pubkey' in network_event, "Network event should have network_pubkey"
    print(f"Network has network_pubkey: {network_event['network_pubkey'][:20]}...")

    # Bootstrap insert network into table (simulating new_network() behavior)
    db.execute(
        """INSERT OR IGNORE INTO networks
           (network_id, all_users_group_id, admins_group_id, creator_user_id, network_pubkey, signed_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (network_id, all_users_group_id, admins_group_id, '', network_event['network_pubkey'],
         peer_shared_id, 1400, peer_id, 1400)
    )

    # Bootstrap insert groups into table
    db.execute(
        """INSERT OR IGNORE INTO groups
           (group_id, name, signed_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (all_users_group_id, 'all_users', peer_shared_id, 1100, peer_id, 1100)
    )
    db.execute(
        """INSERT OR IGNORE INTO groups
           (group_id, name, signed_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (admins_group_id, 'admins', peer_shared_id, 1200, peer_id, 1200)
    )

    # Bootstrap insert channel into table
    db.execute(
        """INSERT OR IGNORE INTO channels
           (channel_id, group_id, name, is_main, signed_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (channel_id, all_users_group_id, 'main', 1, peer_shared_id, 1300, peer_id, 1300)
    )

    db.commit()

    # Create bootstrap user invite
    # Note: Admin privileges are now granted via admin event in new_network(), not via first_peer
    invite_id, invite_private_key, invite_public_key = invite.create_bootstrap_user_invite(
        network_id=network_id,
        network_private_key=network_private_key,
        group_id=all_users_group_id,
        channel_id=channel_id,
        key_id=all_users_key_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=1500,
        db=db
    )
    print(f"Created bootstrap invite: {invite_id[:20]}...")
    print(f"Got invite_private_key: {len(invite_private_key)} bytes")
    print(f"Got invite_public_key: {len(invite_public_key)} bytes")

    # Verify the invite event structure
    invite_blob = store.get(invite_id, db)
    invite_event = crypto.parse_json(invite_blob)

    # Check required fields
    assert invite_event['type'] == 'invite', "Should be invite type"
    assert invite_event['mode'] == 'user', "Should be mode=user"
    assert invite_event['signed_by'] == network_id, f"signed_by should be network_id, got {invite_event.get('signed_by')}"
    assert 'invite_pubkey' in invite_event, "Should have invite_pubkey"
    assert invite_event['group_id'] == all_users_group_id, "Should have correct group_id"
    assert invite_event['channel_id'] == channel_id, "Should have correct channel_id"
    assert invite_event['key_id'] == all_users_key_id, "Should have correct key_id"
    assert invite_event['inviter_peer_shared_id'] == peer_shared_id, "Should have inviter_peer_shared_id"

    print("\nInvite event fields:")
    print(f"  type: {invite_event['type']}")
    print(f"  mode: {invite_event['mode']}")
    print(f"  signed_by: {invite_event['signed_by'][:20]}...")
    print(f"  inviter_peer_shared_id: {invite_event.get('inviter_peer_shared_id', 'None')[:20]}...")

    # Verify signature using network_pubkey
    network_pubkey_bytes = crypto.b64decode(network_event['network_pubkey'])
    assert crypto.verify_event(invite_event, network_pubkey_bytes), \
        "Invite signature should verify with network_pubkey"
    print("\n✓ Signature verified with network_pubkey")

    # Verify invite_private_key matches invite_pubkey in event
    _, derived_public = crypto.generate_keypair()  # Not the right keypair
    invite_pubkey_bytes = crypto.b64decode(invite_event['invite_pubkey'])
    # We can verify the invite keypair is self-consistent by checking if we can sign/verify with it
    test_data = {'test': 'data'}
    signed_test = crypto.sign_event(test_data, invite_private_key)
    assert crypto.verify_event(signed_test, invite_pubkey_bytes), \
        "Invite keypair should be self-consistent (can sign and verify)"
    print("✓ Invite keypair is self-consistent")

    print("\n✅ test_bootstrap_user_invite_signed_by_network passed!")


def test_network_get_public_key():
    """Test that network.get_public_key() returns the correct key."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create a peer
    peer_id, peer_shared_id = peer.create(t_ms=1000, db=db)

    # Create prerequisites
    all_users_group_id, _ = group.create(
        name='all_users', peer_id=peer_id, peer_shared_id=peer_shared_id, t_ms=1100, db=db
    )
    admins_group_id, _ = group.create(
        name='admins', peer_id=peer_id, peer_shared_id=peer_shared_id, t_ms=1200, db=db
    )

    # Create network
    network_id, network_private_key = network.create(
        all_users_group_id=all_users_group_id,
        admins_group_id=admins_group_id,
        creator_user_id='',
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=1400,
        db=db
    )

    # Get network_pubkey from event (network is already projected by network.create())
    network_blob = store.get(network_id, db)
    network_event = crypto.parse_json(network_blob)
    expected_pubkey = crypto.b64decode(network_event['network_pubkey'])

    db.commit()

    # Test get_public_key
    retrieved_pubkey = network.get_public_key(network_id, peer_id, db)
    assert retrieved_pubkey == expected_pubkey, "get_public_key should return correct key"
    print("✓ network.get_public_key() returns correct key")

    # Verify we can sign with private key and verify with retrieved public key
    test_data = {'test': 'verification'}
    signed = crypto.sign_event(test_data, network_private_key)
    assert crypto.verify_event(signed, retrieved_pubkey), \
        "Should be able to verify signature with retrieved public key"
    print("✓ Can sign with network_private_key and verify with get_public_key()")

    print("\n✅ test_network_get_public_key passed!")


if __name__ == '__main__':
    test_bootstrap_user_invite_signed_by_network()
    test_network_get_public_key()
