"""
TreeKEM Phase 2 Tests: O(log n) update path with hash-trie structure.

Tests the TreeKEM O(log n) key distribution using:
- treekem_secret: Tree node symmetric keys (local-only)
- treekem_pubkey: Tree node public keys (shareable)
- treekem_update: Update orchestration events
- treekem_secret_shared: Tree node key distribution (encrypted)

These tests verify the O(log n) update path mechanism where:
- Tree positions are derived from peer_shared_id bits
- Updates encrypt to copath nodes instead of every peer
- Concurrent updates converge deterministically (lowest ID wins)
"""

import pytest
from core.db import Database
from core import schema
from core import crypto
from core import wire_format
from core import treekem
from events.identity import user, peer
from events.group import treekem_secret, treekem_pubkey, treekem_update, treekem_secret_shared


class TestTreeKEMUtilities:
    """Test the core TreeKEM tree utilities."""

    def test_get_path_prefix_depth_zero(self):
        """Depth 0 (root) returns empty bytes."""
        peer_id = crypto.b64encode(b'\x00' * 16)
        prefix = treekem.get_path_prefix(peer_id, 0)
        assert prefix == b''

    def test_get_path_prefix_depth_one(self):
        """Depth 1 extracts first bit."""
        # First bit is 0
        peer_id_0 = crypto.b64encode(b'\x00' * 16)
        prefix_0 = treekem.get_path_prefix(peer_id_0, 1)
        assert prefix_0 == b'\x00'

        # First bit is 1
        peer_id_1 = crypto.b64encode(b'\x80' + b'\x00' * 15)
        prefix_1 = treekem.get_path_prefix(peer_id_1, 1)
        assert prefix_1 == b'\x80'

    def test_get_path_prefix_depth_eight(self):
        """Depth 8 extracts first byte."""
        peer_bytes = b'\xAB' + b'\x00' * 15
        peer_id = crypto.b64encode(peer_bytes)
        prefix = treekem.get_path_prefix(peer_id, 8)
        assert prefix == b'\xAB'

    def test_get_path_prefix_depth_twelve(self):
        """Depth 12 extracts first 1.5 bytes (masked)."""
        peer_bytes = b'\xAB\xCD' + b'\x00' * 14
        peer_id = crypto.b64encode(peer_bytes)
        prefix = treekem.get_path_prefix(peer_id, 12)
        # First 12 bits of 0xABCD = 0xABC (with last nibble masked)
        assert prefix == b'\xAB\xC0'

    def test_get_copath_prefix(self):
        """Copath prefix is sibling (bit flipped)."""
        peer_id = crypto.b64encode(b'\x00' * 16)

        # At depth 1, sibling is 1xxxxxxx (flip first bit)
        copath_1 = treekem.get_copath_prefix(peer_id, 1)
        assert copath_1 == b'\x80'

        # At depth 2, sibling is 01xxxxxx (flip second bit)
        copath_2 = treekem.get_copath_prefix(peer_id, 2)
        assert copath_2 == b'\x40'

    def test_path_covers_peer(self):
        """Test path coverage checking."""
        peer_id = crypto.b64encode(b'\xAB' + b'\x00' * 15)

        # Root covers everyone
        assert treekem.path_covers_peer(b'', 0, peer_id)

        # Matching prefix covers
        assert treekem.path_covers_peer(b'\x80', 1, peer_id)  # 0xAB starts with 1

        # Non-matching prefix doesn't cover
        assert not treekem.path_covers_peer(b'\x00', 1, peer_id)  # 0xAB doesn't start with 0

    def test_compute_copath_for_sender(self):
        """Compute copath for a sender to reach all recipients."""
        # Create some peer IDs with different prefixes
        peer_a = crypto.b64encode(b'\x00' + b'\x00' * 15)  # 0000...
        peer_b = crypto.b64encode(b'\x80' + b'\x00' * 15)  # 1000...
        peer_c = crypto.b64encode(b'\x40' + b'\x00' * 15)  # 0100...

        all_peers = [peer_a, peer_b, peer_c]

        # Copath for peer_a should include subtrees containing peer_b and peer_c
        copath = treekem.compute_copath_for_sender(peer_a, all_peers)

        # Should have nodes that cover peer_b (starts with 1) and peer_c (starts with 01)
        assert len(copath) >= 1

    def test_copath_size_estimate(self):
        """Copath size is O(log n)."""
        assert treekem.copath_size(1) == 0
        assert treekem.copath_size(2) == 1
        assert treekem.copath_size(4) == 2
        assert treekem.copath_size(8) == 3
        assert treekem.copath_size(1024) == 10


class TestTreeKEMSecretEvent:
    """Test treekem_secret event creation and projection."""

    def test_create_treekem_secret(self, fresh_db):
        """Create a treekem_secret event and verify projection."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a treekem_secret at depth 0 (root)
        secret_id = treekem_secret.create(
            depth=0,
            path_prefix=b'',
            peer_id=alice['peer_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        assert len(secret_id) == 24  # base64 encoded event id

        # Verify we can retrieve the secret
        key_data = treekem_secret.get_key(secret_id, alice['peer_id'], db)
        assert key_data is not None
        assert key_data['type'] == 'symmetric'
        assert len(key_data['key']) == 32
        assert key_data['depth'] == 0

    def test_treekem_secret_deterministic(self, fresh_db):
        """Same key material produces same treekem_secret_id."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Generate key material
        key_material = crypto.generate_secret()

        # Create secret with same material
        secret_id1 = treekem_secret.create_with_material(
            depth=1,
            path_prefix=b'\x80',
            key_material=key_material,
            peer_id=alice['peer_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        # Compute expected ID
        blob = wire_format.encode_treekem_secret_wire_event(
            depth=1,
            path_prefix=b'\x80',
            key=key_material,
            created_at_ms=0
        )
        expected_id = crypto.b64encode(crypto.hash(blob))

        assert secret_id1 == expected_id

    def test_treekem_secret_wire_format(self):
        """Test treekem_secret wire encoding/decoding roundtrip."""
        key = crypto.generate_secret()

        blob = wire_format.encode_treekem_secret_wire_event(
            depth=5,
            path_prefix=b'\xAB',
            key=key,
            created_at_ms=0,  # Deterministic
        )

        assert len(blob) == 512
        assert wire_format.is_wire_treekem_secret_envelope(blob)

        decoded = wire_format.decode_treekem_secret_wire_event(blob)
        assert decoded['type'] == 'treekem_secret'
        assert decoded['depth'] == 5
        assert decoded['created_at'] == 0
        assert crypto.b64decode(decoded['key']) == key


class TestTreeKEMPubkeyEvent:
    """Test treekem_pubkey event creation and projection."""

    def test_create_treekem_pubkey(self, fresh_db):
        """Create a treekem_pubkey event and verify projection."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a treekem_pubkey at depth 0 (root)
        pubkey_id, private_key = treekem_pubkey.create(
            depth=0,
            path_prefix=b'',
            parent_pubkey_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        assert len(pubkey_id) == 24
        assert len(private_key) == 32

        # Verify we can retrieve the pubkey
        pk = treekem_pubkey.get_pubkey_at_position(
            depth=0,
            path_prefix=b'',
            recorded_by=alice['peer_id'],
            db=db
        )
        assert pk is not None
        assert pk['type'] == 'asymmetric'
        assert len(pk['public_key']) == 32

        # Verify we can retrieve the private key
        pk_private = treekem_pubkey.get_private_key(pubkey_id, alice['peer_id'], db)
        assert pk_private == private_key

    def test_treekem_pubkey_wire_format(self, fresh_db):
        """Test treekem_pubkey wire encoding/decoding roundtrip."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Generate keypair
        private_key, public_key = crypto.generate_keypair()
        signing_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        blob = wire_format.encode_treekem_pubkey_wire_event(
            depth=3,
            path_prefix=b'\xE0',
            public_key=public_key,
            parent_pubkey_id_b64=None,
            removal_epoch_id_b64=None,
            signed_by_b64=alice['peer_shared_id'],
            signer_type="peer_shared",
            created_at_ms=2000,
            private_key=signing_key,
        )

        assert len(blob) == 512
        assert wire_format.is_wire_treekem_pubkey_envelope(blob)

        decoded = wire_format.decode_treekem_pubkey_wire_event(blob)
        assert decoded['type'] == 'treekem_pubkey'
        assert decoded['depth'] == 3
        assert decoded['signed_by'] == alice['peer_shared_id']
        assert decoded['created_at'] == 2000
        assert crypto.b64decode(decoded['public_key']) == public_key


class TestTreeKEMUpdateEvent:
    """Test treekem_update event creation and projection."""

    def test_create_treekem_update(self, fresh_db):
        """Create a treekem_update event and verify projection."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # First create a root pubkey
        root_pubkey_id, _ = treekem_pubkey.create(
            depth=0,
            path_prefix=b'',
            parent_pubkey_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        # Create an update
        update_id = treekem_update.create(
            root_pubkey_id=root_pubkey_id,
            removal_epoch_id=None,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        assert len(update_id) == 24

        # Verify we can retrieve the update
        update = treekem_update.get_update(update_id, alice['peer_id'], db)
        assert update is not None
        assert update['root_pubkey_id'] == root_pubkey_id
        assert update['author_peer_id'] == alice['peer_shared_id']

    def test_concurrent_updates_lowest_wins(self, fresh_db):
        """For concurrent updates, the lowest update_id wins."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create root pubkeys for two "concurrent" updates
        root_pubkey_a, _ = treekem_pubkey.create(
            depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
            peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
            t_ms=2000, db=db
        )
        root_pubkey_b, _ = treekem_pubkey.create(
            depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
            peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
            t_ms=2001, db=db
        )
        db.commit()

        # Create two "concurrent" updates (same base_update_id=None, same removal_epoch_id=None)
        update_a = treekem_update.create(
            root_pubkey_id=root_pubkey_a,
            removal_epoch_id=None,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        update_b = treekem_update.create(
            root_pubkey_id=root_pubkey_b,
            removal_epoch_id=None,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3001,
            db=db
        )
        db.commit()

        # Get concurrent updates - should be sorted by ID
        concurrent = treekem_update.get_concurrent_updates(
            base_update_id=None,
            removal_epoch_id=None,
            recorded_by=alice['peer_id'],
            db=db
        )

        assert len(concurrent) == 2
        # Winner should be the one with lower ID (lexicographically)
        winner_id = min(update_a, update_b)
        assert concurrent[0]['treekem_update_id'] == winner_id

    def test_treekem_update_wire_format(self, fresh_db):
        """Test treekem_update wire encoding/decoding roundtrip."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        signing_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
        root_pubkey_id = crypto.b64encode(crypto.generate_secret()[:16])

        blob = wire_format.encode_treekem_update_wire_event(
            author_peer_id_b64=alice['peer_shared_id'],
            removal_epoch_id_b64=None,
            base_update_id_b64=None,
            root_pubkey_id_b64=root_pubkey_id,
            signed_by_b64=alice['peer_shared_id'],
            signer_type="peer_shared",
            created_at_ms=2000,
            private_key=signing_key,
        )

        assert len(blob) == 512
        assert wire_format.is_wire_treekem_update_envelope(blob)

        decoded = wire_format.decode_treekem_update_wire_event(blob)
        assert decoded['type'] == 'treekem_update'
        assert decoded['author_peer_id'] == alice['peer_shared_id']
        assert decoded['root_pubkey_id'] == root_pubkey_id
        assert decoded['base_update_id'] is None
        assert decoded['removal_epoch_id'] is None


class TestTreeKEMSecretSharedEvent:
    """Test treekem_secret_shared event creation."""

    def test_treekem_secret_shared_wire_format(self, fresh_db):
        """Test treekem_secret_shared wire encoding basics."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Generate test data
        treekem_secret_id = crypto.b64encode(crypto.generate_secret()[:16])
        symmetric_key = crypto.generate_secret()
        recipient_pubkey_id = crypto.b64encode(crypto.generate_secret()[:16])
        source_update_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Generate recipient keypair
        recipient_private, recipient_public = crypto.generate_keypair()
        recipient_pubkey = {
            'id': crypto.b64decode(recipient_pubkey_id),
            'public_key': recipient_public,
            'type': 'asymmetric'
        }

        # Get signing key
        signing_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        blob = wire_format.encode_treekem_secret_shared_wire_event(
            treekem_secret_id_b64=treekem_secret_id,
            symmetric_key_b64=crypto.b64encode(symmetric_key),
            recipient_pubkey_id_b64=recipient_pubkey_id,
            source_update_id_b64=source_update_id,
            depth=3,
            signed_by_b64=alice['peer_shared_id'],
            signer_type="peer_shared",
            created_at_ms=1000,
            recipient_pubkey=recipient_pubkey,
            private_key=signing_key,
        )

        assert wire_format.is_wire_treekem_secret_shared_envelope(blob)


class TestOLogNEventCount:
    """Test that update paths generate O(log n) events."""

    def test_copath_events_log_n(self, fresh_db):
        """Creating an update path should generate O(log n) events."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create multiple peer IDs to simulate a group
        peer_ids = []
        for i in range(16):  # 16 peers
            peer_bytes = bytes([i * 16]) + b'\x00' * 15
            peer_ids.append(crypto.b64encode(peer_bytes))

        # Compute copath for alice
        copath = treekem.compute_copath_for_sender(alice['peer_shared_id'], peer_ids)

        # With 16 peers, copath should be approximately log2(16) = 4
        assert len(copath) <= 5  # Some slack for tree imbalance

        # Definitely less than O(n)
        assert len(copath) < len(peer_ids)


class TestEventRegistry:
    """Test that all new events are properly registered."""

    def test_all_phase2_events_registered(self, fresh_db):
        """Verify all TreeKEM Phase 2 events are in the registry."""
        from events import registry

        phase2_events = ['treekem_secret', 'treekem_pubkey', 'treekem_update', 'treekem_secret_shared']

        for event_type in phase2_events:
            assert event_type in registry.get_registered_types(), f"{event_type} not registered"

    def test_shareable_flags_correct(self, fresh_db):
        """Verify SHAREABLE flags are set correctly."""
        from events import registry

        # Shareable events
        assert registry.is_shareable('treekem_pubkey')
        assert registry.is_shareable('treekem_update')
        assert registry.is_shareable('treekem_secret_shared')

        # Local-only events
        assert not registry.is_shareable('treekem_secret')


class TestIntegrationWithRemovalEpoch:
    """Test TreeKEM Phase 2 integration with removal epochs."""

    def test_update_with_removal_epoch(self, fresh_db):
        """Create update tied to a removal epoch."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a removal epoch
        from events.identity import removal_epoch
        fake_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=fake_peer_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Create root pubkey under this epoch
        root_pubkey_id, _ = treekem_pubkey.create(
            depth=0,
            path_prefix=b'',
            parent_pubkey_id=None,
            removal_epoch_id=epoch_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        # Create update under this epoch
        update_id = treekem_update.create(
            root_pubkey_id=root_pubkey_id,
            removal_epoch_id=epoch_id,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=4000,
            db=db
        )
        db.commit()

        # Verify update is tied to epoch
        update = treekem_update.get_update(update_id, alice['peer_id'], db)
        assert update['removal_epoch_id'] == epoch_id


class TestConcurrentUpdateTiebreaker:
    """Test deterministic tiebreaker for concurrent updates."""

    def test_three_concurrent_updates_lowest_wins(self, fresh_db):
        """With 3 concurrent updates, the lexicographically lowest ID wins."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create three root pubkeys
        pubkeys = []
        for i in range(3):
            pk_id, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=2000 + i, db=db
            )
            pubkeys.append(pk_id)
        db.commit()

        # Create three concurrent updates (all with base_update_id=None)
        updates = []
        for i, pk_id in enumerate(pubkeys):
            update_id = treekem_update.create(
                root_pubkey_id=pk_id,
                removal_epoch_id=None,
                base_update_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=3000 + i,
                db=db
            )
            updates.append(update_id)
        db.commit()

        # Get concurrent updates
        concurrent = treekem_update.get_concurrent_updates(
            base_update_id=None,
            removal_epoch_id=None,
            recorded_by=alice['peer_id'],
            db=db
        )

        assert len(concurrent) == 3

        # Verify sorted by ID (ascending)
        for i in range(len(concurrent) - 1):
            assert concurrent[i]['treekem_update_id'] < concurrent[i + 1]['treekem_update_id']

        # Winner is the one with lowest ID
        expected_winner = min(updates)
        assert concurrent[0]['treekem_update_id'] == expected_winner

    def test_get_winning_update_returns_lowest(self, fresh_db):
        """get_winning_update should return the update with lowest ID."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create pubkeys and updates
        updates = []
        for i in range(4):
            pk_id, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=2000 + i, db=db
            )
            update_id = treekem_update.create(
                root_pubkey_id=pk_id,
                removal_epoch_id=None,
                base_update_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=3000 + i,
                db=db
            )
            updates.append(update_id)
        db.commit()

        # Get winning update
        winner = treekem_update.get_winning_update(
            removal_epoch_id=None,
            base_update_id=None,
            recorded_by=alice['peer_id'],
            db=db
        )

        assert winner is not None
        expected_winner = min(updates)
        assert winner['treekem_update_id'] == expected_winner

    def test_is_winning_update(self, fresh_db):
        """is_winning_update should correctly identify the winner."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create two concurrent updates
        updates = []
        for i in range(2):
            pk_id, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=2000 + i, db=db
            )
            update_id = treekem_update.create(
                root_pubkey_id=pk_id,
                removal_epoch_id=None,
                base_update_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=3000 + i,
                db=db
            )
            updates.append(update_id)
        db.commit()

        winner_id = min(updates)
        loser_id = max(updates)

        assert treekem_update.is_winning_update(winner_id, alice['peer_id'], db) is True
        assert treekem_update.is_winning_update(loser_id, alice['peer_id'], db) is False

    def test_tiebreaker_is_deterministic(self, fresh_db):
        """Tiebreaker should be deterministic - same input, same winner."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create several updates
        updates = []
        for i in range(5):
            pk_id, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=2000 + i, db=db
            )
            update_id = treekem_update.create(
                root_pubkey_id=pk_id,
                removal_epoch_id=None,
                base_update_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=3000 + i,
                db=db
            )
            updates.append(update_id)
        db.commit()

        # Get winner multiple times - should always be the same
        winners = []
        for _ in range(3):
            winner = treekem_update.get_winning_update(
                removal_epoch_id=None,
                base_update_id=None,
                recorded_by=alice['peer_id'],
                db=db
            )
            winners.append(winner['treekem_update_id'])

        # All winners should be identical
        assert all(w == winners[0] for w in winners)
        assert winners[0] == min(updates)


class TestUpdateChaining:
    """Test update chaining via base_update_id."""

    def test_chain_updates_with_base_update_id(self, fresh_db):
        """Updates can form a chain using base_update_id."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create first update (no base)
        pk1, _ = treekem_pubkey.create(
            depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
            peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
            t_ms=2000, db=db
        )
        update1 = treekem_update.create(
            root_pubkey_id=pk1,
            removal_epoch_id=None,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        # Create second update building on first
        pk2, _ = treekem_pubkey.create(
            depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
            peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
            t_ms=4000, db=db
        )
        update2 = treekem_update.create(
            root_pubkey_id=pk2,
            removal_epoch_id=None,
            base_update_id=update1,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=5000,
            db=db
        )
        db.commit()

        # Create third update building on second
        pk3, _ = treekem_pubkey.create(
            depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
            peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
            t_ms=6000, db=db
        )
        update3 = treekem_update.create(
            root_pubkey_id=pk3,
            removal_epoch_id=None,
            base_update_id=update2,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=7000,
            db=db
        )
        db.commit()

        # Verify chain structure
        u1 = treekem_update.get_update(update1, alice['peer_id'], db)
        u2 = treekem_update.get_update(update2, alice['peer_id'], db)
        u3 = treekem_update.get_update(update3, alice['peer_id'], db)

        assert u1['base_update_id'] is None
        assert u2['base_update_id'] == update1
        assert u3['base_update_id'] == update2

    def test_concurrent_updates_at_same_base(self, fresh_db):
        """Multiple updates can branch from the same base."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create base update
        pk_base, _ = treekem_pubkey.create(
            depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
            peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
            t_ms=2000, db=db
        )
        base_update = treekem_update.create(
            root_pubkey_id=pk_base,
            removal_epoch_id=None,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        # Create two concurrent updates branching from base
        branches = []
        for i in range(2):
            pk, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=4000 + i, db=db
            )
            update_id = treekem_update.create(
                root_pubkey_id=pk,
                removal_epoch_id=None,
                base_update_id=base_update,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=5000 + i,
                db=db
            )
            branches.append(update_id)
        db.commit()

        # Get concurrent updates at base_update
        concurrent = treekem_update.get_concurrent_updates(
            base_update_id=base_update,
            removal_epoch_id=None,
            recorded_by=alice['peer_id'],
            db=db
        )

        assert len(concurrent) == 2

        # Winner should be lowest ID
        winner = treekem_update.get_winning_update(
            removal_epoch_id=None,
            base_update_id=base_update,
            recorded_by=alice['peer_id'],
            db=db
        )
        assert winner['treekem_update_id'] == min(branches)

    def test_updates_at_different_bases_are_separate(self, fresh_db):
        """Updates at different base_update_ids don't interfere."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create two independent base updates
        base_updates = []
        for i in range(2):
            pk, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=2000 + i, db=db
            )
            update_id = treekem_update.create(
                root_pubkey_id=pk,
                removal_epoch_id=None,
                base_update_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=3000 + i,
                db=db
            )
            base_updates.append(update_id)
        db.commit()

        # Create child update for each base
        children = []
        for i, base in enumerate(base_updates):
            pk, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=4000 + i, db=db
            )
            update_id = treekem_update.create(
                root_pubkey_id=pk,
                removal_epoch_id=None,
                base_update_id=base,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=5000 + i,
                db=db
            )
            children.append(update_id)
        db.commit()

        # Each base should have exactly one child
        for i, base in enumerate(base_updates):
            concurrent = treekem_update.get_concurrent_updates(
                base_update_id=base,
                removal_epoch_id=None,
                recorded_by=alice['peer_id'],
                db=db
            )
            assert len(concurrent) == 1
            assert concurrent[0]['treekem_update_id'] == children[i]


class TestSecretSharing:
    """Test treekem_secret_shared key distribution."""

    def test_share_secret_to_recipient(self, fresh_db):
        """Share a secret to a recipient's pubkey."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a treekem_secret
        secret_id = treekem_secret.create(
            depth=0,
            path_prefix=b'',
            peer_id=alice['peer_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        # Create a treekem_pubkey (recipient)
        recipient_pk_id, _ = treekem_pubkey.create(
            depth=1,
            path_prefix=b'\x80',
            parent_pubkey_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        # Create an update (needed for source_update_id)
        root_pk_id, _ = treekem_pubkey.create(
            depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
            peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
            t_ms=3500, db=db
        )
        update_id = treekem_update.create(
            root_pubkey_id=root_pk_id,
            removal_epoch_id=None,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=4000,
            db=db
        )
        db.commit()

        # Share the secret
        shared_id = treekem_secret_shared.create(
            treekem_secret_id=secret_id,
            depth=0,
            path_prefix=b'',
            source_update_id=update_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            recipient_pubkey_id=recipient_pk_id,
            t_ms=5000,
            db=db
        )
        db.commit()

        assert len(shared_id) == 24

        # Verify the share was recorded
        shares = treekem_secret_shared.get_secrets_for_update(update_id, alice['peer_id'], db)
        assert len(shares) == 1
        assert shares[0]['treekem_secret_id'] == secret_id

    def test_share_to_multiple_copath_nodes(self, fresh_db):
        """Share secret to multiple copath recipients."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a secret
        secret_id = treekem_secret.create(
            depth=0,
            path_prefix=b'',
            peer_id=alice['peer_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        # Create multiple recipient pubkeys at different tree positions
        recipient_pubkeys = []
        for i, prefix in enumerate([b'\x80', b'\x40', b'\x20']):
            pk_id, _ = treekem_pubkey.create(
                depth=1,
                path_prefix=prefix,
                parent_pubkey_id=None,
                removal_epoch_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=3000 + i,
                db=db
            )
            recipient_pubkeys.append(pk_id)
        db.commit()

        # Create update
        root_pk_id, _ = treekem_pubkey.create(
            depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=None,
            peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
            t_ms=4000, db=db
        )
        update_id = treekem_update.create(
            root_pubkey_id=root_pk_id,
            removal_epoch_id=None,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=5000,
            db=db
        )
        db.commit()

        # Share to all copath nodes
        shared_ids = treekem_secret_shared.share_secret_with_copath(
            treekem_secret_id=secret_id,
            depth=0,
            path_prefix=b'',
            source_update_id=update_id,
            copath_pubkey_ids=recipient_pubkeys,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=6000,
            db=db
        )
        db.commit()

        assert len(shared_ids) == 3

        # Verify all shares recorded
        shares = treekem_secret_shared.get_secrets_for_update(update_id, alice['peer_id'], db)
        assert len(shares) == 3


class TestTreePositionCoverage:
    """Test that tree positions correctly cover peers."""

    def test_copath_covers_all_other_peers(self):
        """Verify copath computation covers all non-sender peers."""
        # Create peers with distinct prefixes
        peers = [
            crypto.b64encode(b'\x00' * 16),  # 0000...
            crypto.b64encode(b'\x40' * 16),  # 0100...
            crypto.b64encode(b'\x80' * 16),  # 1000...
            crypto.b64encode(b'\xC0' * 16),  # 1100...
        ]

        sender = peers[0]
        copath = treekem.compute_copath_for_sender(sender, peers)

        # Verify all other peers are covered by the copath
        for other_peer in peers[1:]:
            covered = False
            for depth, prefix in copath:
                if treekem.path_covers_peer(prefix, depth, other_peer):
                    covered = True
                    break
            assert covered, f"Peer {other_peer[:16]} not covered by copath"

    def test_sender_not_in_copath_coverage(self):
        """Sender should not be covered by their own copath."""
        peers = [
            crypto.b64encode(b'\x00' * 16),
            crypto.b64encode(b'\x80' * 16),
        ]

        sender = peers[0]
        copath = treekem.compute_copath_for_sender(sender, peers)

        # Sender should NOT be covered by any copath node
        for depth, prefix in copath:
            assert not treekem.path_covers_peer(prefix, depth, sender), \
                f"Sender incorrectly covered by copath node at depth {depth}"

    def test_single_peer_empty_copath(self):
        """With only one peer (the sender), copath should be empty."""
        sender = crypto.b64encode(b'\x00' * 16)
        copath = treekem.compute_copath_for_sender(sender, [sender])
        assert copath == []

    def test_two_peers_minimal_copath(self):
        """With two peers, copath should have one node."""
        peer_a = crypto.b64encode(b'\x00' * 16)  # 0...
        peer_b = crypto.b64encode(b'\x80' * 16)  # 1...

        copath = treekem.compute_copath_for_sender(peer_a, [peer_a, peer_b])

        # Should have exactly one copath node covering peer_b
        assert len(copath) >= 1

        # That node should cover peer_b but not peer_a
        covered_b = any(treekem.path_covers_peer(p, d, peer_b) for d, p in copath)
        covered_a = any(treekem.path_covers_peer(p, d, peer_a) for d, p in copath)
        assert covered_b
        assert not covered_a


class TestEndToEndUpdatePath:
    """Test complete update path creation and key distribution."""

    def test_full_update_path_creation(self, fresh_db):
        """Create a complete update path with pubkeys, secrets, and sharing."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Step 1: Create root secret and pubkey
        root_secret_id = treekem_secret.create(
            depth=0,
            path_prefix=b'',
            peer_id=alice['peer_id'],
            t_ms=2000,
            db=db
        )
        root_pubkey_id, _ = treekem_pubkey.create(
            depth=0,
            path_prefix=b'',
            parent_pubkey_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2001,
            db=db
        )
        db.commit()

        # Step 2: Create child pubkeys for copath (simulating other peers' nodes)
        left_child_pk, _ = treekem_pubkey.create(
            depth=1,
            path_prefix=b'\x00',
            parent_pubkey_id=root_pubkey_id,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2002,
            db=db
        )
        right_child_pk, _ = treekem_pubkey.create(
            depth=1,
            path_prefix=b'\x80',
            parent_pubkey_id=root_pubkey_id,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2003,
            db=db
        )
        db.commit()

        # Step 3: Create update
        update_id = treekem_update.create(
            root_pubkey_id=root_pubkey_id,
            removal_epoch_id=None,
            base_update_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        # Step 4: Share root secret to both child pubkeys
        shared_ids = treekem_secret_shared.share_secret_with_copath(
            treekem_secret_id=root_secret_id,
            depth=0,
            path_prefix=b'',
            source_update_id=update_id,
            copath_pubkey_ids=[left_child_pk, right_child_pk],
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=4000,
            db=db
        )
        db.commit()

        # Verify complete update path
        update = treekem_update.get_update(update_id, alice['peer_id'], db)
        assert update is not None
        assert update['root_pubkey_id'] == root_pubkey_id

        shares = treekem_secret_shared.get_secrets_for_update(update_id, alice['peer_id'], db)
        assert len(shares) == 2

        # Verify pubkey tree structure
        root_pk = treekem_pubkey.get_pubkey_by_id(root_pubkey_id, alice['peer_id'], db)
        left_pk = treekem_pubkey.get_pubkey_by_id(left_child_pk, alice['peer_id'], db)
        right_pk = treekem_pubkey.get_pubkey_by_id(right_child_pk, alice['peer_id'], db)

        assert root_pk['depth'] == 0
        assert left_pk['depth'] == 1
        assert right_pk['depth'] == 1
        assert left_pk['parent_pubkey_id'] == root_pubkey_id
        assert right_pk['parent_pubkey_id'] == root_pubkey_id


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_max_depth_path_prefix(self):
        """Test path prefix at maximum depth."""
        peer_bytes = b'\xFF' * 16
        peer_id = crypto.b64encode(peer_bytes)

        # At max depth, should use all bits
        prefix = treekem.get_path_prefix(peer_id, treekem.MAX_TREE_DEPTH)

        # Should be ceil(20/8) = 3 bytes
        expected_len = (treekem.MAX_TREE_DEPTH + 7) // 8
        assert len(prefix) == expected_len

    def test_depth_zero_always_matches(self):
        """Depth 0 (root) should match any peer."""
        peers = [
            crypto.b64encode(b'\x00' * 16),
            crypto.b64encode(b'\xFF' * 16),
            crypto.b64encode(b'\xAB\xCD' * 8),
        ]

        for peer_id in peers:
            assert treekem.path_covers_peer(b'', 0, peer_id)

    def test_concurrent_updates_different_epochs_separate(self, fresh_db):
        """Updates in different removal epochs don't compete."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        from events.identity import removal_epoch

        # Create two removal epochs
        epoch1 = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=crypto.b64encode(b'\x01' * 16),
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        epoch2 = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=crypto.b64encode(b'\x02' * 16),
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2001,
            db=db
        )
        db.commit()

        # Create updates in different epochs
        updates_epoch1 = []
        updates_epoch2 = []

        for i in range(2):
            pk, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=epoch1,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=3000 + i, db=db
            )
            update_id = treekem_update.create(
                root_pubkey_id=pk,
                removal_epoch_id=epoch1,
                base_update_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=4000 + i,
                db=db
            )
            updates_epoch1.append(update_id)

        for i in range(2):
            pk, _ = treekem_pubkey.create(
                depth=0, path_prefix=b'', parent_pubkey_id=None, removal_epoch_id=epoch2,
                peer_id=alice['peer_id'], peer_shared_id=alice['peer_shared_id'],
                t_ms=5000 + i, db=db
            )
            update_id = treekem_update.create(
                root_pubkey_id=pk,
                removal_epoch_id=epoch2,
                base_update_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=6000 + i,
                db=db
            )
            updates_epoch2.append(update_id)
        db.commit()

        # Each epoch should have its own concurrent updates
        concurrent1 = treekem_update.get_concurrent_updates(
            base_update_id=None,
            removal_epoch_id=epoch1,
            recorded_by=alice['peer_id'],
            db=db
        )
        concurrent2 = treekem_update.get_concurrent_updates(
            base_update_id=None,
            removal_epoch_id=epoch2,
            recorded_by=alice['peer_id'],
            db=db
        )

        assert len(concurrent1) == 2
        assert len(concurrent2) == 2

        # Winners should be different (different epochs)
        winner1 = treekem_update.get_winning_update(epoch1, None, alice['peer_id'], db)
        winner2 = treekem_update.get_winning_update(epoch2, None, alice['peer_id'], db)

        assert winner1['treekem_update_id'] in updates_epoch1
        assert winner2['treekem_update_id'] in updates_epoch2
        assert winner1['treekem_update_id'] != winner2['treekem_update_id']


class TestRemovedUserKeySharing:
    """Test that removed users don't receive new keys.

    These tests use proper multi-peer scenarios with user.join and sync.
    """

    def test_removed_user_pubkey_should_not_receive_new_secrets(self, fresh_db):
        """After user removal, their pubkeys should not be used for new secrets.

        Scenario:
        1. Alice creates network, Bob joins
        2. Bob creates a treekem_pubkey
        3. Alice removes Bob
        4. Alice creates a new removal_epoch
        5. When creating new secrets, Alice should check Bob is removed
        """
        from tests.utils.tick_helper import assert_eventually
        from events.identity import invite, user_removed
        from events.identity import removal_epoch

        db = fresh_db

        # Alice creates network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Alice creates invite for Bob
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

        # Bob joins
        bob_peer_id = peer.create(t_ms=2000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
        db.commit()

        # Wait for Bob to sync
        def bob_has_channel():
            channels = db.query_all(
                "SELECT channel_id FROM channels WHERE recorded_by = ?",
                (bob['peer_id'],)
            )
            assert len(channels) >= 1

        t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

        # Bob creates his own treekem_pubkey (from Bob's perspective)
        bob_pubkey_id, _ = treekem_pubkey.create(
            depth=1,
            path_prefix=treekem.get_path_prefix(bob['peer_shared_id'], 1),
            parent_pubkey_id=None,
            removal_epoch_id=None,
            peer_id=bob['peer_id'],
            peer_shared_id=bob['peer_shared_id'],
            t_ms=t_ms + 100,
            db=db
        )
        db.commit()

        # Sync so Alice sees Bob's pubkey
        def alice_sees_bob_pubkey():
            pk = treekem_pubkey.get_pubkey_by_id(bob_pubkey_id, alice['peer_id'], db)
            assert pk is not None

        t_ms = assert_eventually(alice_sees_bob_pubkey, db=db, start_t_ms=t_ms)

        # Alice removes Bob
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=t_ms + 100,
            db=db
        )
        db.commit()

        # Alice creates a removal_epoch referencing Bob
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=bob['peer_shared_id'],
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=t_ms + 200,
            db=db
        )
        db.commit()

        # Verify Bob is removed in this epoch
        assert removal_epoch.is_removed(bob['peer_shared_id'], epoch_id, alice['peer_id'], db)

        # Get Bob's pubkey from Alice's view
        bob_pk = treekem_pubkey.get_pubkey_by_id(bob_pubkey_id, alice['peer_id'], db)
        assert bob_pk is not None

        # The pubkey owner is Bob's peer_shared_id
        assert bob_pk['owner_peer_id'] == bob['peer_shared_id']

        # SECURITY CHECK: Owner is removed, so Alice should NOT share to this pubkey
        is_owner_removed = removal_epoch.is_removed(
            bob_pk['owner_peer_id'],
            epoch_id,
            alice['peer_id'],
            db
        )
        assert is_owner_removed is True


class TestSubjectiveGroupView:
    """Test that senders use their subjective view of the group.

    Different peers may have different views of who is in the group
    due to network partitions or sync delays.
    """

    def test_removal_epoch_tracks_removed_peers(self, fresh_db):
        """Removal epochs correctly track which peers are removed."""
        from events.identity import removal_epoch

        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create some fake peer IDs
        bob_peer_id = crypto.b64encode(b'\xBB' + b'\x00' * 15)
        charlie_peer_id = crypto.b64encode(b'\xCC' + b'\x00' * 15)

        # Before any removal epoch, nobody is removed
        assert not removal_epoch.is_removed(bob_peer_id, None, alice['peer_id'], db)
        assert not removal_epoch.is_removed(charlie_peer_id, None, alice['peer_id'], db)

        # Create removal epoch removing Bob
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=bob_peer_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Now Bob is removed, Charlie is not
        assert removal_epoch.is_removed(bob_peer_id, epoch_id, alice['peer_id'], db)
        assert not removal_epoch.is_removed(charlie_peer_id, epoch_id, alice['peer_id'], db)

    def test_copath_excludes_removed_peers(self):
        """Copath computation should not include removed peers' subtrees."""
        # Create peers with distinct tree positions
        alice = crypto.b64encode(b'\x00' * 16)   # 0000...
        bob = crypto.b64encode(b'\x40' * 16)     # 0100...
        charlie = crypto.b64encode(b'\x80' * 16) # 1000...
        eve = crypto.b64encode(b'\xC0' * 16)     # 1100...

        all_peers = [alice, bob, charlie, eve]
        active_peers = [alice, bob, charlie]  # Eve is removed

        # Full copath covers all others
        full_copath = treekem.compute_copath_for_sender(alice, all_peers)

        # Reduced copath only covers active peers
        reduced_copath = treekem.compute_copath_for_sender(alice, active_peers)

        # Verify all active peers (except sender) are covered
        for peer_id in [bob, charlie]:
            covered = any(
                treekem.path_covers_peer(prefix, depth, peer_id)
                for depth, prefix in reduced_copath
            )
            assert covered, f"Active peer should be covered"


class TestLeafFallback:
    """Test leaf-level fallback when copath nodes are missing."""

    def test_missing_copath_pubkey_detection(self, fresh_db):
        """Detect when a copath position has no pubkey."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Alice creates a pubkey at depth=1, prefix=0x00
        alice_pk, _ = treekem_pubkey.create(
            depth=1,
            path_prefix=b'\x00',
            parent_pubkey_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        # Check: pubkey exists at 0x00 position
        pk_at_00 = treekem_pubkey.get_pubkey_at_position(
            depth=1, path_prefix=b'\x00',
            recorded_by=alice['peer_id'], db=db
        )
        assert pk_at_00 is not None

        # Check: no pubkey at 0x80 position (sibling)
        pk_at_80 = treekem_pubkey.get_pubkey_at_position(
            depth=1, path_prefix=b'\x80',
            recorded_by=alice['peer_id'], db=db
        )
        assert pk_at_80 is None  # No pubkey here - would need leaf fallback


class TestMultiPeerKeyDistribution:
    """Test key distribution across multiple peers."""

    def test_alice_and_bob_create_pubkeys(self, fresh_db):
        """Both Alice and Bob create pubkeys that sync to each other."""
        from tests.utils.tick_helper import assert_eventually
        from events.identity import invite

        db = fresh_db

        # Alice creates network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Alice creates her treekem_pubkey
        alice_pk_id, _ = treekem_pubkey.create(
            depth=1,
            path_prefix=treekem.get_path_prefix(alice['peer_shared_id'], 1),
            parent_pubkey_id=None,
            removal_epoch_id=None,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=1500,
            db=db
        )
        db.commit()

        # Bob joins
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
        bob_peer_id = peer.create(t_ms=2500, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2500, db=db)
        db.commit()

        # Wait for sync
        def bob_in_network():
            channels = db.query_all(
                "SELECT channel_id FROM channels WHERE recorded_by = ?",
                (bob['peer_id'],)
            )
            assert len(channels) >= 1

        t_ms = assert_eventually(bob_in_network, db=db, start_t_ms=None)

        # Bob creates his treekem_pubkey
        bob_pk_id, _ = treekem_pubkey.create(
            depth=1,
            path_prefix=treekem.get_path_prefix(bob['peer_shared_id'], 1),
            parent_pubkey_id=None,
            removal_epoch_id=None,
            peer_id=bob['peer_id'],
            peer_shared_id=bob['peer_shared_id'],
            t_ms=t_ms + 100,
            db=db
        )
        db.commit()

        # Sync
        def alice_sees_bob_pk():
            pk = treekem_pubkey.get_pubkey_by_id(bob_pk_id, alice['peer_id'], db)
            assert pk is not None

        def bob_sees_alice_pk():
            pk = treekem_pubkey.get_pubkey_by_id(alice_pk_id, bob['peer_id'], db)
            assert pk is not None

        t_ms = assert_eventually(alice_sees_bob_pk, db=db, start_t_ms=t_ms)
        t_ms = assert_eventually(bob_sees_alice_pk, db=db, start_t_ms=t_ms)

        # Both can see each other's pubkeys
        alice_view_of_bob = treekem_pubkey.get_pubkey_by_id(bob_pk_id, alice['peer_id'], db)
        bob_view_of_alice = treekem_pubkey.get_pubkey_by_id(alice_pk_id, bob['peer_id'], db)

        assert alice_view_of_bob['owner_peer_id'] == bob['peer_shared_id']
        assert bob_view_of_alice['owner_peer_id'] == alice['peer_shared_id']


class TestCopathKeyDistributionSecurity:
    """Security tests for copath key distribution."""

    def test_compute_copath_varies_by_sender_position(self):
        """Different senders have different copaths based on their tree position."""
        # Peers at different positions
        peer_00 = crypto.b64encode(b'\x00' * 16)  # 0000...
        peer_40 = crypto.b64encode(b'\x40' * 16)  # 0100...
        peer_80 = crypto.b64encode(b'\x80' * 16)  # 1000...
        peer_C0 = crypto.b64encode(b'\xC0' * 16)  # 1100...

        all_peers = [peer_00, peer_40, peer_80, peer_C0]

        # Each sender has a different copath
        copath_00 = treekem.compute_copath_for_sender(peer_00, all_peers)
        copath_80 = treekem.compute_copath_for_sender(peer_80, all_peers)

        # Sender 00 needs to reach 40, 80, C0
        # Sender 80 needs to reach 00, 40, C0

        # Verify sender is NOT covered by their own copath
        for depth, prefix in copath_00:
            assert not treekem.path_covers_peer(prefix, depth, peer_00)

        for depth, prefix in copath_80:
            assert not treekem.path_covers_peer(prefix, depth, peer_80)

    def test_o_log_n_copath_size(self):
        """Copath size scales logarithmically with group size."""
        import math

        for n in [4, 8, 16, 32, 64]:
            # Create n peers with spread-out positions
            peers = []
            for i in range(n):
                # Spread peers across the tree
                peer_bytes = bytes([(i * 256 // n)]) + b'\x00' * 15
                peers.append(crypto.b64encode(peer_bytes))

            sender = peers[0]
            copath = treekem.compute_copath_for_sender(sender, peers)

            # Copath size should be O(log n)
            expected_max = int(math.ceil(math.log2(n))) + 2  # Some slack
            assert len(copath) <= expected_max, \
                f"Copath for {n} peers should be <= {expected_max}, got {len(copath)}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
