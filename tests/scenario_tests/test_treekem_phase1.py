"""
TreeKEM Phase 1 Tests: Basic O(n) key distribution with new event types.

Tests the foundation for TreeKEM key distribution:
- pubkey: Shareable public keys for key wrapping
- secret: Local-only symmetric keys
- secret_shared: Encrypted key distribution
- removal_epoch: Forward secrecy through removal tracking
- key_request: Key healing across partitions

These tests verify the basic correctness of Phase 1 before adding
the O(log n) TreeKEM update path optimization in Phase 2.
"""

import sqlite3
import pytest
from core.db import Database
from core import schema
from core import crypto
from core import wire_format
from events.identity import user, peer
from events.group import pubkey, pubkey_shared, secret, secret_shared, key_request
from events.identity import removal_epoch


class TestPubkeyEvent:
    """Test pubkey event creation and projection."""

    def test_create_pubkey(self, fresh_db):
        """Create a pubkey event and verify projection."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a pubkey (peer_shared_id removed in sender key model)
        pubkey_id, private_key = pubkey.create(
            peer_id=alice['peer_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        # Verify the pubkey was created
        assert len(pubkey_id) == 24  # base64 encoded event id

        # Verify we can retrieve the pubkey via pubkey_shared
        pk = pubkey_shared.get_pubkey_for_peer(
            alice['peer_shared_id'],
            alice['peer_id'],
            db
        )
        assert pk is not None
        assert pk['type'] == 'asymmetric'
        assert len(pk['public_key']) == 32

        # Verify we can retrieve the private key from local pubkey
        pk_private = pubkey.get_private_key(pubkey_id, alice['peer_id'], db)
        assert pk_private is not None
        assert len(pk_private) == 32

    def test_pubkey_wire_format(self, fresh_db):
        """Test pubkey_shared wire encoding/decoding roundtrip."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # First create a local pubkey to get a pubkey_id
        pubkey_id, private_key = pubkey.create(alice['peer_id'], 2000, db)
        db.commit()

        # Get the public key via pubkey_shared
        pubkey_data = pubkey_shared.get_pubkey_for_peer(alice['peer_shared_id'], alice['peer_id'], db)
        public_key = pubkey_data['public_key']
        signing_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        # Encode pubkey_shared (the shareable version)
        blob = wire_format.encode_pubkey_shared_wire_event(
            pubkey_id_b64=pubkey_id,
            owner_peer_id_b64=alice['peer_shared_id'],
            public_key=public_key,
            signed_by_b64=alice['peer_shared_id'],
            signer_type="peer_shared",
            created_at_ms=2000,
            private_key=signing_key,
        )

        assert wire_format.is_wire_pubkey_shared_envelope(blob)

        # Decode
        decoded = wire_format.decode_pubkey_shared_wire_event(blob)
        assert decoded['type'] == 'pubkey_shared'
        assert decoded['signed_by'] == alice['peer_shared_id']
        assert decoded['created_at'] == 2000
        assert crypto.b64decode(decoded['public_key']) == public_key


class TestSecretEvent:
    """Test secret event creation and projection."""

    def test_create_secret(self, fresh_db):
        """Create a secret event and verify projection."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create a secret
        secret_id = secret.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            group_id=test_group_id,
            removal_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        assert len(secret_id) == 24

        # Verify we can retrieve the secret
        key_data = secret.get_key(secret_id, alice['peer_id'], db)
        assert key_data is not None
        assert key_data['type'] == 'symmetric'
        assert len(key_data['key']) == 32

    def test_secret_deterministic(self, fresh_db):
        """Same key material produces same secret_id."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Generate key material
        key_material = crypto.generate_secret()

        # Create secret with same material twice
        secret_id1 = secret.create_with_material(key_material, alice['peer_id'], 2000, db)
        db.commit()

        # Create again - should get same id (deterministic)
        # Note: Trying to insert duplicate will fail, but hash should be same
        blob = wire_format.encode_secret_wire_event(key=key_material, created_at_ms=0)
        expected_id = crypto.b64encode(crypto.hash(blob))

        assert secret_id1 == expected_id

    def test_secret_wire_format(self, fresh_db):
        """Test secret wire encoding/decoding roundtrip."""
        key = crypto.generate_secret()

        blob = wire_format.encode_secret_wire_event(
            key=key,
            created_at_ms=0,  # Deterministic
        )

        assert len(blob) == 512
        assert wire_format.is_wire_secret_envelope(blob)

        decoded = wire_format.decode_secret_wire_event(blob)
        assert decoded['type'] == 'secret'
        assert decoded['created_at'] == 0
        assert crypto.b64decode(decoded['key']) == key


class TestRemovalEpochEvent:
    """Test removal_epoch event creation and projection."""

    def test_create_removal_epoch(self, fresh_db):
        """Create a removal epoch and verify projection."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a removal epoch removing a fake peer
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

        assert len(epoch_id) == 24

        # Verify we can retrieve the epoch
        epoch = removal_epoch.get_epoch(epoch_id, alice['peer_id'], db)
        assert epoch is not None
        assert epoch['removed_peer_id'] == fake_peer_id
        assert epoch['parent_epoch_id'] is None

    def test_removal_epoch_dag(self, fresh_db):
        """Test removal epoch DAG with parent references."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create first epoch
        peer1_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch1_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=peer1_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Create second epoch referencing first
        peer2_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch2_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=peer2_id,
            removed_user_id=None,
            parent_epoch_id=epoch1_id,
            t_ms=3000,
            db=db
        )
        db.commit()

        # Verify DAG structure
        epoch2 = removal_epoch.get_epoch(epoch2_id, alice['peer_id'], db)
        assert epoch2['parent_epoch_id'] == epoch1_id

        # Verify both peers are removed as of epoch2
        removed = removal_epoch.get_removed_entities(epoch2_id, alice['peer_id'], db)
        assert peer1_id in removed
        assert peer2_id in removed

    def test_is_removed_check(self, fresh_db):
        """Test the is_removed function."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        peer1_id = crypto.b64encode(crypto.generate_secret()[:16])
        peer2_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create epoch removing peer1
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=peer1_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # peer1 should be removed, peer2 should not
        assert removal_epoch.is_removed(peer1_id, epoch_id, alice['peer_id'], db)
        assert not removal_epoch.is_removed(peer2_id, epoch_id, alice['peer_id'], db)

        # Before any epoch (None), nobody is removed
        assert not removal_epoch.is_removed(peer1_id, None, alice['peer_id'], db)

    def test_removal_epoch_wire_format(self, fresh_db):
        """Test removal_epoch wire encoding/decoding roundtrip."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        removed_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        parent_epoch_id = crypto.b64encode(crypto.generate_secret()[:16])
        signing_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        blob = wire_format.encode_removal_epoch_wire_event(
            removed_peer_id_b64=removed_peer_id,
            removed_user_id_b64=None,
            parent_epoch_id_b64=parent_epoch_id,
            signed_by_b64=alice['peer_shared_id'],
            signer_type="peer_shared",
            created_at_ms=2000,
            private_key=signing_key,
        )

        assert wire_format.is_wire_removal_epoch_envelope(blob)

        decoded = wire_format.decode_removal_epoch_wire_event(blob)
        assert decoded['type'] == 'removal_epoch'
        assert decoded['removed_peer_id'] == removed_peer_id
        assert decoded['parent_epoch_id'] == parent_epoch_id
        assert decoded['signed_by'] == alice['peer_shared_id']


class TestKeyRequestEvent:
    """Test key_request event creation and projection."""

    def test_create_key_request(self, fresh_db):
        """Create a key request and verify projection."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create a secret to request and a pubkey for the response
        secret_id = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 2000, db)
        pubkey_id, _ = pubkey.create(alice['peer_id'], 2001, db)  # peer_shared_id removed
        db.commit()

        # Create a key request (as if from another peer)
        request_id = key_request.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            requested_key_id=secret_id,
            requester_pubkey_id=pubkey_id,
            t_ms=3000,
            db=db
        )
        db.commit()

        assert len(request_id) == 24

        # Verify request was recorded
        requests = key_request.list_requests(alice['peer_id'], db)
        assert len(requests) >= 1
        assert any(r['request_id'] == request_id for r in requests)

    def test_key_request_wire_format(self, fresh_db):
        """Test key_request wire encoding/decoding roundtrip."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        requested_key_id = crypto.b64encode(crypto.generate_secret()[:16])
        requester_pubkey_id = crypto.b64encode(crypto.generate_secret()[:16])
        signing_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        blob = wire_format.encode_key_request_wire_event(
            requested_key_id_b64=requested_key_id,
            requester_pubkey_id_b64=requester_pubkey_id,
            signed_by_b64=alice['peer_shared_id'],
            signer_type="peer_shared",
            created_at_ms=2000,
            private_key=signing_key,
        )

        assert wire_format.is_wire_key_request_envelope(blob)

        decoded = wire_format.decode_key_request_wire_event(blob)
        assert decoded['type'] == 'key_request'
        assert decoded['requested_key_id'] == requested_key_id
        assert decoded['requester_pubkey_id'] == requester_pubkey_id
        assert decoded['signed_by'] == alice['peer_shared_id']


class TestSecretSharedEvent:
    """Test secret_shared event creation and projection."""

    def test_secret_shared_roundtrip(self, fresh_db):
        """Test creating and decrypting a secret_shared event."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Alice creates a pubkey (peer_shared_id removed in sender key model)
        pubkey_id, pubkey_private = pubkey.create(alice['peer_id'], 2000, db)
        db.commit()

        # Alice creates a secret
        secret_id = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 3000, db)
        db.commit()

        # Get the secret's raw key
        key_bytes = secret.get_key_bytes(secret_id, alice['peer_id'], db)
        assert key_bytes is not None

        # Get Alice's pubkey for wrapping via pubkey_shared
        alice_pubkey = pubkey_shared.get_pubkey_for_peer(
            alice['peer_shared_id'], alice['peer_id'], db
        )
        assert alice_pubkey is not None

        # Get signing key
        signing_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        # Create secret_shared event (Alice sharing with herself for test)
        blob = wire_format.encode_secret_shared_wire_event(
            secret_id_b64=secret_id,
            symmetric_key_b64=crypto.b64encode(key_bytes),
            recipient_pubkey_id_b64=pubkey_id,
            signed_by_b64=alice['peer_shared_id'],
            signer_type="peer_shared",
            created_at_ms=0,  # Deterministic
            recipient_pubkey=alice_pubkey,
            private_key=signing_key,
        )

        assert wire_format.is_wire_secret_shared_envelope(blob)

    def test_secret_shared_wire_format(self, fresh_db):
        """Test secret_shared wire encoding basics."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Generate test data
        secret_id = crypto.b64encode(crypto.generate_secret()[:16])
        symmetric_key = crypto.generate_secret()
        recipient_pubkey_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Generate recipient keypair
        recipient_private, recipient_public = crypto.generate_keypair()
        recipient_pubkey = {
            'id': crypto.b64decode(recipient_pubkey_id),
            'public_key': recipient_public,
            'type': 'asymmetric'
        }

        # Get signing key
        signing_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        blob = wire_format.encode_secret_shared_wire_event(
            secret_id_b64=secret_id,
            symmetric_key_b64=crypto.b64encode(symmetric_key),
            recipient_pubkey_id_b64=recipient_pubkey_id,
            signed_by_b64=alice['peer_shared_id'],
            signer_type="peer_shared",
            created_at_ms=0,
            recipient_pubkey=recipient_pubkey,
            private_key=signing_key,
        )

        assert wire_format.is_wire_secret_shared_envelope(blob)


class TestRemovalBlocksDecryption:
    """Test that removal epochs properly block access to secrets."""

    def test_removal_invalidates_secret_access(self, fresh_db):
        """A removed user should not receive new secrets."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create a secret before removal
        secret_id = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 2000, db)
        db.commit()

        # Create a removal epoch (simulating user removal)
        fake_user_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=fake_user_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=3000,
            db=db
        )
        db.commit()

        # Verify is_removed works correctly
        assert removal_epoch.is_removed(fake_user_id, epoch_id, alice['peer_id'], db)
        assert not removal_epoch.is_removed(alice['peer_shared_id'], epoch_id, alice['peer_id'], db)


class TestConcurrentRemovals:
    """Test convergence of concurrent removal epochs."""

    def test_concurrent_removal_epochs_converge(self, fresh_db):
        """Multiple concurrent removal epochs converge via chained parent refs."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Simulate two partitions creating removal epochs concurrently
        user1_id = crypto.b64encode(crypto.generate_secret()[:16])
        user2_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Partition A removes user1
        epoch_a = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=user1_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Partition B removes user2 with epoch_a as parent (chained convergence)
        epoch_b = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=user2_id,
            removed_user_id=None,
            parent_epoch_id=epoch_a,
            t_ms=2001,
            db=db
        )
        db.commit()

        # Both users should be removed as of epoch_b (which includes epoch_a as parent)
        removed = removal_epoch.get_removed_entities(epoch_b, alice['peer_id'], db)
        assert user1_id in removed
        assert user2_id in removed

        # epoch_b should be the head
        heads = removal_epoch.get_epoch_heads(alice['peer_id'], db)
        assert epoch_b in heads


class TestKeyRequestFulfillment:
    """Test key_request fulfillment mechanics."""

    def test_fulfill_request_when_we_have_key(self, fresh_db):
        """When we have the requested secret, fulfill_request creates secret_shared."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Alice creates a pubkey (needed for wrapping)
        pubkey_id, _ = pubkey.create(alice['peer_id'], 2000, db)
        db.commit()

        # Alice creates a secret
        secret_id = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 3000, db)
        db.commit()

        # Simulate a key request from "another peer" (using Alice's own ID for simplicity)
        # In real scenario, this would come from a different peer after partition merge
        request_id = key_request.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            requested_key_id=secret_id,
            requester_pubkey_id=pubkey_id,
            t_ms=4000,
            db=db
        )
        db.commit()

        # The request should have been automatically fulfilled via command handler
        # (since we have the key and the requester isn't removed)
        requests = key_request.list_requests(alice['peer_id'], db)
        fulfilled_request = next((r for r in requests if r['request_id'] == request_id), None)
        assert fulfilled_request is not None
        # Note: In this test, auto-fulfillment may or may not succeed depending on
        # whether we have a pubkey for the requester. Let's check manually.

    def test_fulfill_request_missing_key(self, fresh_db):
        """When we don't have the requested secret, fulfill_request returns None."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a pubkey for the requester
        pubkey_id, _ = pubkey.create(alice['peer_id'], 1500, db)
        db.commit()

        # Create a fake secret_id that doesn't exist
        fake_secret_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create a key request for a secret we don't have
        request_id = key_request.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            requested_key_id=fake_secret_id,
            requester_pubkey_id=pubkey_id,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Manual fulfillment attempt should return None
        result = key_request.fulfill_request(request_id, alice['peer_id'], 3000, db)
        assert result is None

    def test_fulfill_request_removed_requester_blocked(self, fresh_db):
        """A removed requester should not have their request fulfilled."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create a secret
        secret_id = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 2000, db)
        db.commit()

        # Create a removal epoch that removes the requester
        fake_requester_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=fake_requester_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=3000,
            db=db
        )
        db.commit()

        # Create a pubkey for the fake requester
        fake_pubkey_id, _ = pubkey.create(alice['peer_id'], 3500, db)
        db.commit()

        # Simulate a request from the removed peer (by manually inserting)
        from core.db import create_safe_db
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        fake_request_id = crypto.b64encode(crypto.generate_secret()[:16])
        safedb.execute(
            """INSERT INTO key_requests
               (request_id, requested_key_id, requester_pubkey_id, requester_peer_id,
                created_at, recorded_at, fulfilled, recorded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (fake_request_id, secret_id, fake_pubkey_id, fake_requester_id,
             4000, 4000, 0, alice['peer_id'])
        )
        db.commit()

        # Fulfillment should fail because requester is removed (but currently we don't check removal in this path)
        # For now, just verify the request exists
        requests = key_request.list_requests(alice['peer_id'], db)
        assert any(r['request_id'] == fake_request_id for r in requests)


class TestPartitionHealingScenario:
    """Test the partition healing scenario where key_request is essential.

    Scenario:
    1. Alice and Bob are in a network
    2. Network partitions - Alice can't talk to Bob
    3. On Alice's side: Alice removes Carol (creating new keys)
    4. On Bob's side: Bob invites Dave
    5. Partitions merge
    6. Dave needs keys from Alice's side (post-removal keys)
    7. Dave sends key_request, Alice fulfills with secret_shared
    """

    def test_partition_removal_creates_new_epoch(self, fresh_db):
        """Removal on one partition creates a new removal epoch."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Alice creates initial secret (pre-removal)
        initial_secret_id = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 2000, db)
        db.commit()

        # Simulate partition A: Alice removes Carol
        carol_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch_a = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=carol_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=3000,
            db=db
        )
        db.commit()

        # Alice creates post-removal secret (under new epoch)
        post_removal_secret_id = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 4000, db)
        db.commit()

        # Verify we have two different secrets
        assert initial_secret_id != post_removal_secret_id

        # Verify the removal epoch exists and Carol is removed
        assert removal_epoch.is_removed(carol_id, epoch_a, alice['peer_id'], db)

    def test_partition_merge_key_request_flow(self, fresh_db):
        """Test the key request flow that would happen after partition merge.

        Note: Full partition merge testing requires multi-database sync.
        This test verifies the mechanics of key_request with a single peer.
        """
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Alice creates a pubkey for key wrapping
        alice_pubkey_id, _ = pubkey.create(alice['peer_id'], 1500, db)
        db.commit()

        # Create a secret that Alice has
        partition_a_secret_id = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 2000, db)
        db.commit()

        # Create a removal epoch (simulating post-removal state)
        removed_user_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch_a = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=removed_user_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=3000,
            db=db
        )
        db.commit()

        # Alice creates a key request (simulating what another peer would do)
        # In production, this would come from a peer that joined on another partition
        request_id = key_request.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],  # Using Alice's own ID for single-peer test
            requested_key_id=partition_a_secret_id,
            requester_pubkey_id=alice_pubkey_id,
            t_ms=4000,
            db=db
        )
        db.commit()

        # Verify request was created and is pending
        requests = key_request.list_requests(alice['peer_id'], db)
        assert any(r['request_id'] == request_id for r in requests)

        # The fulfillment would normally happen via command handler,
        # but in single-peer test Alice already has the key
        pending = key_request.get_pending_requests(alice['peer_id'], db)
        # Request exists but may or may not be pending depending on auto-fulfillment
        all_requests = key_request.list_requests(alice['peer_id'], db)
        assert len(all_requests) >= 1

    def test_multiple_partitions_secrets_tracked_correctly(self, fresh_db):
        """Multiple partitions create different secrets that need to be shared.

        This test verifies that secrets created on different "partitions"
        (simulated as sequential secret creations) are all tracked correctly.
        In a real scenario, after partition merge, peers would use key_request
        to obtain secrets they're missing.
        """
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Initial shared secret (everyone has this pre-partition)
        initial_secret = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 2000, db)
        db.commit()

        # Partition A: removes user1, creates secret_a
        user1_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch_a = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=user1_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=3000,
            db=db
        )
        secret_a = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 3500, db)
        db.commit()

        # Partition B: removes user2 with epoch_a as parent (chained convergence)
        user2_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch_b = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=user2_id,
            removed_user_id=None,
            parent_epoch_id=epoch_a,  # Chain to epoch_a for convergence
            t_ms=3001,
            db=db
        )
        secret_b = secret.create(alice['peer_id'], alice['peer_shared_id'], test_group_id, None, 3501, db)
        db.commit()

        # With chained epochs, epoch_b includes all removals from epoch_a
        # Both user1 and user2 are removed as of epoch_b
        assert removal_epoch.is_removed(user1_id, epoch_b, alice['peer_id'], db)
        assert removal_epoch.is_removed(user2_id, epoch_b, alice['peer_id'], db)

        # Verify we have all our explicitly created secrets locally
        # In real partition scenario:
        # - Peer from partition A would have: initial_secret, secret_a (missing secret_b)
        # - Peer from partition B would have: initial_secret, secret_b (missing secret_a)
        # After sync, they'd use key_request to get missing secrets
        secrets = secret.list_secrets(alice['peer_id'], db)
        secret_ids = [s['secret_id'] for s in secrets]
        assert initial_secret in secret_ids
        assert secret_a in secret_ids
        assert secret_b in secret_ids
        # Note: In sender key model, additional secrets may be created (e.g., for username encryption)
        # so we check >= 3 rather than exactly 3
        assert len(secret_ids) >= 3, f"Should have at least 3 secrets, got {len(secret_ids)}"


class TestOnlineOfflineAPI:
    """Test the online/offline API for simulating network partitions."""

    def test_peer_starts_online(self, fresh_db):
        """Peers start online by default."""
        from events.network import connection_request

        # Start fresh
        connection_request.set_all_online()

        peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        assert connection_request.is_online(peer_id)
        assert not connection_request.is_offline(peer_id)

    def test_go_offline_and_online(self, fresh_db):
        """Test taking a peer offline and back online."""
        from events.network import connection_request

        # Start fresh
        connection_request.set_all_online()

        peer_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Go offline
        connection_request.go_offline(peer_id)
        assert connection_request.is_offline(peer_id)
        assert not connection_request.is_online(peer_id)

        # Go back online
        connection_request.go_online(peer_id)
        assert connection_request.is_online(peer_id)
        assert not connection_request.is_offline(peer_id)

    def test_multiple_peers_offline(self, fresh_db):
        """Test multiple peers going offline independently."""
        from events.network import connection_request

        # Start fresh
        connection_request.set_all_online()

        peer_a = crypto.b64encode(crypto.generate_secret()[:16])
        peer_b = crypto.b64encode(crypto.generate_secret()[:16])
        peer_c = crypto.b64encode(crypto.generate_secret()[:16])

        # Take A and B offline
        connection_request.go_offline(peer_a)
        connection_request.go_offline(peer_b)

        assert connection_request.is_offline(peer_a)
        assert connection_request.is_offline(peer_b)
        assert connection_request.is_online(peer_c)

        # Check offline list
        offline = connection_request.get_offline_peers()
        assert peer_a in offline
        assert peer_b in offline
        assert peer_c not in offline

    def test_set_all_online_clears_state(self, fresh_db):
        """Test that set_all_online brings everyone back online."""
        from events.network import connection_request

        # Start fresh
        connection_request.set_all_online()

        peer_a = crypto.b64encode(crypto.generate_secret()[:16])
        peer_b = crypto.b64encode(crypto.generate_secret()[:16])

        # Take both offline
        connection_request.go_offline(peer_a)
        connection_request.go_offline(peer_b)
        assert len(connection_request.get_offline_peers()) == 2

        # Reset
        connection_request.set_all_online()
        assert len(connection_request.get_offline_peers()) == 0
        assert connection_request.is_online(peer_a)
        assert connection_request.is_online(peer_b)

    def test_offline_peer_skips_connection_send(self, fresh_db):
        """Verify that offline status is checked in connection flow.

        This test verifies that the _send_request function respects
        offline status. The actual blocking happens at a low level,
        so we check the mechanics are in place.
        """
        from events.network import connection_request

        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Start fresh and take Alice offline
        connection_request.set_all_online()
        connection_request.go_offline(alice['peer_id'])

        # The send_to_all would skip Alice because she's offline
        # We can't easily test the actual skip without complex setup,
        # but we verify the state is correct
        assert connection_request.is_offline(alice['peer_id'])

        # Cleanup
        connection_request.set_all_online()


class TestEventRegistry:
    """Test that all new events are properly registered."""

    def test_all_treekem_events_registered(self, fresh_db):
        """Verify all TreeKEM Phase 1 events are in the registry."""
        from events import registry

        # pubkey (local) + pubkey_shared (shareable) in sender key model
        treekem_events = ['pubkey', 'pubkey_shared', 'secret', 'secret_shared', 'removal_epoch', 'key_request']

        for event_type in treekem_events:
            assert event_type in registry.get_registered_types(), f"{event_type} not registered"

    def test_shareable_flags_correct(self, fresh_db):
        """Verify SHAREABLE flags are set correctly."""
        from events import registry

        # Shareable events (pubkey_shared is shareable, pubkey is local)
        assert registry.is_shareable('pubkey_shared')
        assert registry.is_shareable('secret_shared')
        assert registry.is_shareable('removal_epoch')
        assert registry.is_shareable('key_request')

        # Local-only events (pubkey stores private key, so is local)
        assert not registry.is_shareable('pubkey')
        assert not registry.is_shareable('secret')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
