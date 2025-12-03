"""Unit tests for sync_connect invite signature authentication.

Tests the bootstrap authentication flow where a joiner uses their invite
private key to sign sync_connect, allowing connection before peer_shared syncs.
"""
import pytest
import json
import sqlite3
from db import Database, create_safe_db, create_unsafe_db
import schema
import crypto
import store
from events.identity import user, peer, invite
from events.network import sync_connect, transit_key
import queues


@pytest.fixture
def setup_alice_bob():
    """Create Alice's network and Bob joins via invite link."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    alice_peer_id = alice['peer_id']

    alice_safedb = create_safe_db(db, recorded_by=alice_peer_id)
    alice_self = alice_safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (alice_peer_id, alice_peer_id)
    )
    alice_peer_shared_id = alice_self['peer_shared_id']

    # Alice creates invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice_peer_id,
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']

    db.commit()

    return {
        'db': db,
        'alice_peer_id': alice_peer_id,
        'alice_peer_shared_id': alice_peer_shared_id,
        'bob_peer_id': bob_peer_id,
        'bob_peer_shared_id': bob_peer_shared_id,
        'invite_id': invite_id,
        'invite_link': invite_link,
    }


def test_sync_connect_has_invite_id(setup_alice_bob):
    """Test that sync_connect event includes invite_id when sent by joiner."""
    data = setup_alice_bob
    db = data['db']

    # Bob sends sync_connect to Alice
    sync_connect.send(
        to_peer_shared_id=data['alice_peer_shared_id'],
        from_peer_id=data['bob_peer_id'],
        from_peer_shared_id=data['bob_peer_shared_id'],
        invite_id=data['invite_id'],
        t_ms=3000,
        db=db
    )
    db.commit()

    # Drain the queued blob
    blobs = queues.incoming.drain(100, 3001, db)
    assert len(blobs) == 1, "Should have one queued blob"

    # Unwrap to get event data (Alice receives)
    unwrapped, missing = crypto.unwrap_transit(blobs[0], data['alice_peer_id'], db)
    assert unwrapped is not None, "Alice should be able to unwrap the blob"

    event_data = crypto.parse_json(unwrapped)
    assert event_data.get('type') == 'sync_connect'
    assert event_data.get('invite_id') == data['invite_id'], "sync_connect should include invite_id"


def test_sync_connect_has_invite_signature(setup_alice_bob):
    """Test that sync_connect event includes invite_signature when joiner has invite key."""
    data = setup_alice_bob
    db = data['db']

    # Bob sends sync_connect to Alice
    sync_connect.send(
        to_peer_shared_id=data['alice_peer_shared_id'],
        from_peer_id=data['bob_peer_id'],
        from_peer_shared_id=data['bob_peer_shared_id'],
        invite_id=data['invite_id'],
        t_ms=3000,
        db=db
    )
    db.commit()

    # Drain and unwrap
    blobs = queues.incoming.drain(100, 3001, db)
    unwrapped, _ = crypto.unwrap_transit(blobs[0], data['alice_peer_id'], db)
    event_data = crypto.parse_json(unwrapped)

    assert 'invite_signature' in event_data, "sync_connect should include invite_signature"
    assert event_data['invite_signature'], "invite_signature should not be empty"


def test_bob_has_invite_private_key(setup_alice_bob):
    """Test that Bob has the invite private key in group_prekeys table."""
    data = setup_alice_bob
    db = data['db']

    bob_safedb = create_safe_db(db, recorded_by=data['bob_peer_id'])

    # Check Bob's group_prekeys
    prekey_row = bob_safedb.query_one(
        "SELECT private_key FROM group_prekeys WHERE owner_peer_id = ? AND recorded_by = ? LIMIT 1",
        (data['bob_peer_id'], data['bob_peer_id'])
    )

    assert prekey_row is not None, "Bob should have a group_prekey"
    assert prekey_row['private_key'] is not None, "Bob should have the private key"


def test_invite_blob_exists_for_alice(setup_alice_bob):
    """Test that Alice can retrieve the invite blob by invite_id."""
    data = setup_alice_bob
    db = data['db']

    unsafedb = create_unsafe_db(db)
    invite_blob = store.get(data['invite_id'], unsafedb)

    assert invite_blob is not None, f"Invite blob should exist for invite_id={data['invite_id'][:20]}..."

    invite_event = crypto.parse_json(invite_blob)
    assert invite_event.get('type') == 'invite'
    assert 'invite_pubkey' in invite_event, "Invite should have invite_pubkey"


def test_invite_signature_verification_manual(setup_alice_bob):
    """Manually test the invite signature verification logic."""
    data = setup_alice_bob
    db = data['db']
    unsafedb = create_unsafe_db(db)

    # Bob sends sync_connect
    sync_connect.send(
        to_peer_shared_id=data['alice_peer_shared_id'],
        from_peer_id=data['bob_peer_id'],
        from_peer_shared_id=data['bob_peer_shared_id'],
        invite_id=data['invite_id'],
        t_ms=3000,
        db=db
    )
    db.commit()

    # Get the queued blob and unwrap as Alice
    blobs = queues.incoming.drain(100, 3001, db)
    unwrapped, _ = crypto.unwrap_transit(blobs[0], data['alice_peer_id'], db)
    event_data = crypto.parse_json(unwrapped)

    # Extract what we need for verification
    invite_id = event_data.get('invite_id')
    invite_signature_b64 = event_data.get('invite_signature')

    print(f"invite_id: {invite_id[:20] if invite_id else 'None'}...")
    print(f"invite_signature_b64: {invite_signature_b64[:20] if invite_signature_b64 else 'None'}...")

    assert invite_id, "invite_id should be present"
    assert invite_signature_b64, "invite_signature should be present"

    # Get invite blob
    invite_blob = store.get(invite_id, unsafedb)
    assert invite_blob, f"invite blob should exist"

    invite_event = crypto.parse_json(invite_blob)
    invite_pubkey_b64 = invite_event.get('invite_pubkey')
    print(f"invite_pubkey from invite: {invite_pubkey_b64[:20] if invite_pubkey_b64 else 'None'}...")

    assert invite_pubkey_b64, "invite should have invite_pubkey"
    invite_public_key = crypto.b64decode(invite_pubkey_b64)

    # Reconstruct what was signed (remove invite_signature field)
    connect_without_invite_sig = {k: v for k, v in event_data.items() if k != 'invite_signature'}
    sig_data = json.dumps(connect_without_invite_sig, sort_keys=True).encode()

    print(f"sig_data: {sig_data[:100]}...")

    invite_signature = crypto.b64decode(invite_signature_b64)

    # Verify
    is_valid = crypto.verify(sig_data, invite_signature, invite_public_key)
    assert is_valid, "Invite signature should verify successfully"


def test_invite_prekey_matches_invite_pubkey(setup_alice_bob):
    """Test that Bob's group_prekey matches the invite's invite_pubkey."""
    data = setup_alice_bob
    db = data['db']
    unsafedb = create_unsafe_db(db)

    # Get invite's public key
    invite_blob = store.get(data['invite_id'], unsafedb)
    invite_event = crypto.parse_json(invite_blob)
    invite_pubkey_b64 = invite_event.get('invite_pubkey')

    # Get Bob's group_prekey
    bob_safedb = create_safe_db(db, recorded_by=data['bob_peer_id'])
    prekey_row = bob_safedb.query_one(
        "SELECT private_key, public_key FROM group_prekeys WHERE owner_peer_id = ? AND recorded_by = ? LIMIT 1",
        (data['bob_peer_id'], data['bob_peer_id'])
    )

    # Derive public key from private key to compare
    if prekey_row and prekey_row['private_key']:
        # Get the public key that corresponds to this private key
        bob_pubkey = prekey_row.get('public_key')
        if bob_pubkey:
            bob_pubkey_b64 = crypto.b64encode(bob_pubkey) if isinstance(bob_pubkey, bytes) else bob_pubkey
            print(f"Invite pubkey: {invite_pubkey_b64}")
            print(f"Bob's prekey pubkey: {bob_pubkey_b64}")

            # They should match if Bob got the right key from the invite
            assert invite_pubkey_b64 == bob_pubkey_b64, "Bob's prekey should match invite's pubkey"


def test_sync_connect_project_authenticates_with_invite(setup_alice_bob):
    """Test that sync_connect.project() successfully authenticates via invite signature."""
    data = setup_alice_bob
    db = data['db']

    # Bob sends sync_connect
    sync_connect.send(
        to_peer_shared_id=data['alice_peer_shared_id'],
        from_peer_id=data['bob_peer_id'],
        from_peer_shared_id=data['bob_peer_shared_id'],
        invite_id=data['invite_id'],
        t_ms=3000,
        db=db
    )
    db.commit()

    # Get blob and store it (simulating what sync.receive does)
    blobs = queues.incoming.drain(100, 3001, db)
    unwrapped, _ = crypto.unwrap_transit(blobs[0], data['alice_peer_id'], db)

    # Store the event
    event_id = store.blob(unwrapped, 3001, True, db)

    # Project as Alice
    result = sync_connect.project(
        event_id=event_id,
        recorded_by=data['alice_peer_id'],
        recorded_at=3001,
        db=db
    )

    assert result is not None, "sync_connect.project should succeed with invite auth"

    # Check connection was stored
    unsafedb = create_unsafe_db(db)
    conn = unsafedb.query_one(
        "SELECT * FROM sync_connections WHERE peer_shared_id = ?",
        (data['bob_peer_shared_id'],)
    )
    assert conn is not None, "Connection should be stored"
    assert conn['their_transit_key'] is not None, "their_transit_key should be stored"
