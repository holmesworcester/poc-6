"""Unit tests for bloom filter salt derivation and consistency."""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite, peer, peer_shared
from events.transit import sync


def test_public_key_consistency():
    """Test that Bob's public key is consistent in his view."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Bob's view of his own public key
    bob_own_pk = peer.get_public_key(bob['peer_id'], bob['peer_id'], db)
    assert bob_own_pk is not None, "Bob should have a public key"


def test_bloom_salt_consistency():
    """Test that bloom salt is consistent for given key."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Get Alice's public key
    alice_pk = peer.get_public_key(alice['peer_id'], alice['peer_id'], db)

    # Derive salts for the same window multiple times
    window_id = 1
    salt1 = sync.derive_salt(alice_pk, window_id)
    salt2 = sync.derive_salt(alice_pk, window_id)

    assert salt1 == salt2, "Bloom salt should be consistent for same key and window"


def test_salt_derivation_different_windows():
    """Test that different windows produce different salts."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Get Alice's public key
    alice_pk = peer.get_public_key(alice['peer_id'], alice['peer_id'], db)

    # Derive salts for different windows
    salt0 = sync.derive_salt(alice_pk, 0)
    salt1 = sync.derive_salt(alice_pk, 1)
    salt2 = sync.derive_salt(alice_pk, 2)

    assert salt0 != salt1, "Different windows should produce different salts"
    assert salt1 != salt2, "Different windows should produce different salts"
    assert salt0 != salt2, "Different windows should produce different salts"
