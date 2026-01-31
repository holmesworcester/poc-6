"""
DH-Based TreeKEM Tests: Verify Diffie-Hellman key exchange enables O(1) root convergence.

Tests the BeeKEM-style DH-based TreeKEM where:
- Parent secrets are derived via DH(my_secret, sibling_pubkey)
- Since DH is symmetric, both siblings compute the SAME parent secret
- All members converge to the same root secret naturally
- O(1) broadcast using root secret reaches everyone

Key insight: DH(a, B) = DH(b, A) - both sides compute identical shared secrets!
"""

import pytest
from core import crypto
from core import treekem
from events.identity import user, peer
from events.group import (
    sender_key,
    secret,
    treekem_pubkey,
    treekem_pubkey_shared,
    treekem_secret,
    treekem_update,
)


class TestECDHKeyExchange:
    """Test the ECDH (X25519) key exchange primitives."""

    def test_x25519_keypair_generation(self, fresh_db):
        """X25519 keypairs should be 32 bytes each."""
        private_key, public_key = crypto.generate_x25519_keypair()

        assert len(private_key) == 32
        assert len(public_key) == 32
        assert private_key != public_key

    def test_ecdh_shared_secret_symmetric(self, fresh_db):
        """DH(a, B) should equal DH(b, A) - the key insight for TreeKEM convergence."""
        # Alice's keypair
        alice_private, alice_public = crypto.generate_x25519_keypair()

        # Bob's keypair
        bob_private, bob_public = crypto.generate_x25519_keypair()

        # Alice computes DH with Bob's public key
        alice_shared = crypto.ecdh_shared_secret(alice_private, bob_public)

        # Bob computes DH with Alice's public key
        bob_shared = crypto.ecdh_shared_secret(bob_private, alice_public)

        # They should be identical!
        assert alice_shared == bob_shared
        assert len(alice_shared) == 32

    def test_ecdh_different_parties_different_secrets(self, fresh_db):
        """Different pairs should produce different shared secrets."""
        alice_private, alice_public = crypto.generate_x25519_keypair()
        bob_private, bob_public = crypto.generate_x25519_keypair()
        carol_private, carol_public = crypto.generate_x25519_keypair()

        alice_bob = crypto.ecdh_shared_secret(alice_private, bob_public)
        alice_carol = crypto.ecdh_shared_secret(alice_private, carol_public)
        bob_carol = crypto.ecdh_shared_secret(bob_private, carol_public)

        assert alice_bob != alice_carol
        assert alice_bob != bob_carol
        assert alice_carol != bob_carol


class TestDHPathSecretDerivation:
    """Test DH-based path secret derivation."""

    def test_derive_dh_path_secret(self, fresh_db):
        """DH shared secret should derive consistent parent secrets."""
        # Generate keypairs for two siblings
        alice_private, alice_public = crypto.generate_x25519_keypair()
        bob_private, bob_public = crypto.generate_x25519_keypair()

        # Both compute DH shared secret
        alice_shared = crypto.ecdh_shared_secret(alice_private, bob_public)
        bob_shared = crypto.ecdh_shared_secret(bob_private, alice_public)

        # Derive parent secret from DH output
        depth = 2
        path_prefix = b'\x80'

        alice_parent = crypto.derive_dh_path_secret(alice_shared, depth, path_prefix)
        bob_parent = crypto.derive_dh_path_secret(bob_shared, depth, path_prefix)

        # Both should derive the SAME parent secret
        assert alice_parent == bob_parent
        assert len(alice_parent) == 32

    def test_different_depths_different_parents(self, fresh_db):
        """Different depths should produce different parent secrets."""
        shared_secret = crypto.generate_secret()
        path_prefix = b'\x80'

        parent_d1 = crypto.derive_dh_path_secret(shared_secret, 1, path_prefix)
        parent_d2 = crypto.derive_dh_path_secret(shared_secret, 2, path_prefix)

        assert parent_d1 != parent_d2


class TestTreeKEMX25519Keypairs:
    """Test TreeKEM with X25519 keypairs for DH."""

    def test_treekem_pubkey_creates_x25519(self, fresh_db):
        """TreeKEM pubkeys should now use X25519 by default."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a treekem_pubkey (uses X25519 by default)
        pubkey_id, pubkey_shared_id, private_key = treekem_pubkey.create(
            depth=1,
            path_prefix=b'\x80',
            parent_pubkey_shared_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db,
        )
        db.commit()

        # Should have valid IDs
        assert pubkey_id is not None
        assert pubkey_shared_id is not None
        assert len(private_key) == 32

        # Public key should also be 32 bytes (X25519 format)
        public_key = treekem_pubkey.get_public_key(pubkey_id, alice['peer_id'], db)
        assert len(public_key) == 32


class TestSiblingResolution:
    """Test resolution finding for blank sibling nodes."""

    def test_get_resolution_with_pubkey(self, fresh_db):
        """Resolution of non-blank node should return just that pubkey."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a pubkey at depth=1
        pubkey_id, pubkey_shared_id, _ = treekem_pubkey.create(
            depth=1,
            path_prefix=b'\x80',
            parent_pubkey_shared_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db,
        )
        db.commit()

        # Resolution should find this pubkey
        resolution = treekem_pubkey_shared.get_resolution(
            depth=1,
            path_prefix=b'\x80',
            max_depth=4,
            recorded_by=alice['peer_id'],
            db=db,
            removal_epoch_id=None,
        )

        assert len(resolution) == 1
        assert resolution[0]['id'] == crypto.b64decode(pubkey_id)

    def test_get_resolution_blank_node(self, fresh_db):
        """Resolution of blank node should find descendants."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create pubkeys at depth=2 (children of depth=1)
        # Left child (prefix \x00)
        pk1_id, _, _ = treekem_pubkey.create(
            depth=2,
            path_prefix=b'\x00',
            parent_pubkey_shared_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db,
        )

        # Right child (prefix \x40 - bit 1 set at position 1)
        pk2_id, _, _ = treekem_pubkey.create(
            depth=2,
            path_prefix=b'\x40',
            parent_pubkey_shared_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2100,
            db=db,
        )
        db.commit()

        # Resolution at depth=1 (blank) should find both children
        resolution = treekem_pubkey_shared.get_resolution(
            depth=1,
            path_prefix=b'\x00',
            max_depth=4,
            recorded_by=alice['peer_id'],
            db=db,
            removal_epoch_id=None,
        )

        # Should find both depth=2 pubkeys as resolution
        assert len(resolution) == 2


class TestDHBasedUpdatePath:
    """Test DH-based update path creation."""

    def test_create_update_path_dh_single_member(self, fresh_db):
        """Single member update path should create pubkeys and secrets."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create DH-based update path
        result = treekem_update.create_update_path_dh(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removal_epoch_id=None,
            base_update_id=None,
            active_members=[alice['peer_shared_id']],
            t_ms=2000,
            db=db,
        )
        db.commit()

        # Should have created update
        assert result['treekem_update_id'] is not None

        # Should have path pubkeys (root to leaf)
        assert len(result['path_pubkeys']) >= 1

        # For single member, no secrets shared to others
        # (no siblings to share with)

    def test_create_update_path_dh_creates_root_secret(self, fresh_db):
        """DH update path should create root secret at depth=0."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create update path
        result = treekem_update.create_update_path_dh(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removal_epoch_id=None,
            base_update_id=None,
            active_members=[alice['peer_shared_id']],
            t_ms=2000,
            db=db,
        )
        db.commit()

        # Should have root secret
        root_key = treekem_secret.get_root_secret(alice['peer_id'], db)
        assert root_key is not None
        assert len(root_key) == 32


class TestO1BroadcastWithDH:
    """Test that O(1) broadcast works with DH-based TreeKEM."""

    def test_sender_can_use_root_broadcast(self, fresh_db):
        """After DH update, sender should be able to broadcast with root secret."""
        from events.group import secret_broadcast

        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create DH update path (establishes root secret)
        treekem_update.create_update_path_dh(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removal_epoch_id=None,
            base_update_id=None,
            active_members=[alice['peer_shared_id']],
            t_ms=2000,
            db=db,
        )
        db.commit()

        # Create a sender key
        secret_id = secret.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            group_id=test_group_id,
            removal_epoch_id=None,
            t_ms=3000,
            db=db,
        )
        db.commit()

        # Verify root secret exists
        root_key = treekem_secret.get_root_secret_key_data(alice['peer_id'], db)
        assert root_key is not None

        # Should be able to create broadcast using root
        broadcast_id = secret_broadcast.create(
            secret_id=secret_id,
            source_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=4000,
            db=db,
        )
        db.commit()

        assert broadcast_id is not None


class TestDHRootConvergence:
    """Test that DH-based TreeKEM achieves root convergence."""

    def test_siblings_converge_to_same_parent_secret(self, fresh_db):
        """Two siblings using DH should derive the same parent secret."""
        # This is a unit test of the convergence property

        # Simulate Alice at leaf position
        alice_private, alice_public = crypto.generate_x25519_keypair()

        # Simulate Bob at sibling leaf position
        bob_private, bob_public = crypto.generate_x25519_keypair()

        depth = 2
        path_prefix = b'\x00'

        # Alice computes parent secret using Bob's pubkey
        alice_dh = crypto.ecdh_shared_secret(alice_private, bob_public)
        alice_parent = crypto.derive_dh_path_secret(alice_dh, depth, path_prefix)

        # Bob computes parent secret using Alice's pubkey
        bob_dh = crypto.ecdh_shared_secret(bob_private, alice_public)
        bob_parent = crypto.derive_dh_path_secret(bob_dh, depth, path_prefix)

        # Both should have the SAME parent secret!
        assert alice_parent == bob_parent

        # And they can continue deriving up to root
        root_from_alice = crypto.derive_path_secret(alice_parent, 1, path_prefix)
        root_from_bob = crypto.derive_path_secret(bob_parent, 1, path_prefix)

        assert root_from_alice == root_from_bob


class TestTreeKEMJobs:
    """Test TreeKEM update and re-update jobs."""

    def test_should_update_respects_join_delay(self, fresh_db):
        """Peers should wait after join before updating."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Immediately after join, should not update (within 10s delay)
        should = treekem_update.should_update(alice['peer_id'], 2000, db)
        assert should is False

        # After join delay (10 seconds), should update
        should = treekem_update.should_update(alice['peer_id'], 15000, db)
        assert should is True

    def test_should_update_respects_rotation_interval(self, fresh_db):
        """Peers should not update too frequently."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Verify we can update initially (after join delay)
        should = treekem_update.should_update(alice['peer_id'], 15000, db)
        assert should is True

        # First update after join delay - use t_ms that will be stored
        update_time = 15000
        treekem_update.create_update_path_dh(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removal_epoch_id=None,
            base_update_id=None,
            active_members=[alice['peer_shared_id']],
            t_ms=update_time,
            db=db,
        )
        db.commit()

        # Check the update was recorded
        latest = treekem_update.get_latest_update_by_author(
            alice['peer_shared_id'], alice['peer_id'], db
        )
        assert latest is not None
        # The recorded_at will be around update_time
        recorded_at = latest['recorded_at']

        # Shortly after recorded_at, should NOT update again
        should = treekem_update.should_update(alice['peer_id'], recorded_at + 1000, db)
        assert should is False, "Should not update within rotation interval"

        # After rotation interval (5 minutes = 300,000ms), should update
        should = treekem_update.should_update(alice['peer_id'], recorded_at + 300_001, db)
        assert should is True, "Should update after rotation interval"


class TestConcurrentUpdateHandling:
    """Test handling of concurrent updates (re-update mechanism)."""

    def test_is_winning_update(self, fresh_db):
        """Lowest update ID should win among concurrent updates."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create first update
        result1 = treekem_update.create_update_path_dh(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removal_epoch_id=None,
            base_update_id=None,
            active_members=[alice['peer_shared_id']],
            t_ms=15000,
            db=db,
        )
        db.commit()

        update1_id = result1['treekem_update_id']

        # This single update should be the winner
        is_winner = treekem_update.is_winning_update(update1_id, alice['peer_id'], db)
        assert is_winner is True

    def test_get_concurrent_updates(self, fresh_db):
        """Should find all updates with the same base_update_id."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create first update (base_update_id = None)
        result1 = treekem_update.create_update_path_dh(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removal_epoch_id=None,
            base_update_id=None,
            active_members=[alice['peer_shared_id']],
            t_ms=15000,
            db=db,
        )
        db.commit()

        # Get concurrent updates at base=None
        concurrent = treekem_update.get_concurrent_updates(
            base_update_id=None,
            removal_epoch_id=None,
            recorded_by=alice['peer_id'],
            db=db,
        )

        # Should find the one update
        assert len(concurrent) == 1
        assert concurrent[0]['treekem_update_id'] == result1['treekem_update_id']
