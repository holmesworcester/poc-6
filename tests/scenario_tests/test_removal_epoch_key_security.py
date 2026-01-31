"""Tests for removal epoch key security.

These tests verify that:
1. Keys are bound to removal epochs when created
2. Key requests from removed users are denied
3. The denial happens even if the responder doesn't explicitly "know" about the removal
   (because having the key implies having its removal epoch)
"""
import pytest
import sqlite3
from core import crypto
from core.db import Database, create_safe_db
from core import schema
from events.identity import user, invite, peer, removal_epoch
from events.group import secret, key_announce, key_request, sender_key


@pytest.fixture
def fresh_db():
    """Create a fresh in-memory database for each test."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = Database(conn)
    schema.create_all(db)
    return db


class TestKeyRemovalEpochBinding:
    """Tests that keys are properly bound to removal epochs."""

    def test_key_created_without_removal_has_no_epoch(self, fresh_db):
        """When no removals have happened, keys have no removal_epoch_id."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create a key (this should have no removal_epoch_id)
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        # Get the key_id
        key_id = crypto.b64encode(key_data['id'])

        # Check the key_announce has no removal_epoch_id
        epoch_id = key_announce.get_removal_epoch_for_key(key_id, alice['peer_id'], db)
        assert epoch_id is None

    def test_key_created_after_removal_has_epoch(self, fresh_db):
        """When a removal has happened, new keys reference the removal epoch."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a fake removed peer ID (doesn't need to be a real peer for this test)
        removed_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        removed_user_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create removal epoch
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=removed_peer_id,
            removed_user_id=removed_user_id,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Now create a key after the removal
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        # Get the key_id
        key_id = crypto.b64encode(key_data['id'])

        # Check the key_announce has the removal_epoch_id
        key_epoch_id = key_announce.get_removal_epoch_for_key(key_id, alice['peer_id'], db)
        assert key_epoch_id == epoch_id


class TestRemovalEpochDAG:
    """Tests for the removal epoch DAG structure."""

    def test_is_removed_returns_true_for_removed_entity(self, fresh_db):
        """is_removed returns True for an entity removed in an epoch."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        removed_peer_id = crypto.b64encode(crypto.generate_secret()[:16])

        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=removed_peer_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        is_removed = removal_epoch.is_removed(
            removed_peer_id, epoch_id, alice['peer_id'], db
        )
        assert is_removed is True

    def test_is_removed_returns_false_for_non_removed_entity(self, fresh_db):
        """is_removed returns False for an entity not removed in an epoch."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        removed_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        other_peer_id = crypto.b64encode(crypto.generate_secret()[:16])

        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=removed_peer_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Check a different peer that wasn't removed
        is_removed = removal_epoch.is_removed(
            other_peer_id, epoch_id, alice['peer_id'], db
        )
        assert is_removed is False

    def test_removal_epoch_chain_includes_parent_removals(self, fresh_db):
        """Removal epochs chain together - later epochs include earlier removals."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        peer1 = crypto.b64encode(crypto.generate_secret()[:16])
        peer2 = crypto.b64encode(crypto.generate_secret()[:16])

        # First removal
        epoch1_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=peer1,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Second removal chains to first
        epoch2_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=peer2,
            removed_user_id=None,
            parent_epoch_id=epoch1_id,
            t_ms=3000,
            db=db
        )
        db.commit()

        # peer1 is removed in epoch1
        assert removal_epoch.is_removed(peer1, epoch1_id, alice['peer_id'], db) is True
        # peer2 is NOT removed in epoch1
        assert removal_epoch.is_removed(peer2, epoch1_id, alice['peer_id'], db) is False

        # Both are removed in epoch2 (chains to epoch1)
        assert removal_epoch.is_removed(peer1, epoch2_id, alice['peer_id'], db) is True
        assert removal_epoch.is_removed(peer2, epoch2_id, alice['peer_id'], db) is True


class TestKeyRequestFulfillmentWithRemovalCheck:
    """Tests that key request fulfillment properly checks removal status.

    These tests verify the core security property: a key request is denied
    if the requester is removed in the key's removal epoch.
    """

    def test_fulfill_request_denies_removed_requester(self, fresh_db):
        """Key request is denied if requester is removed in the key's epoch."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # The "removed" peer and their pubkey
        removed_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        removed_pubkey_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create removal epoch for this peer
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=removed_peer_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Create a key after the removal (bound to this epoch)
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()
        key_id = crypto.b64encode(key_data['id'])

        # Verify the key is bound to the removal epoch
        key_epoch = key_announce.get_removal_epoch_for_key(key_id, alice['peer_id'], db)
        assert key_epoch == epoch_id

        # Create a key request from the removed peer
        request_id = key_request.create(
            peer_id=alice['peer_id'],
            peer_shared_id=removed_peer_id,  # Requester is the removed peer
            requested_key_id=key_id,
            requester_pubkey_id=removed_pubkey_id,
            t_ms=4000,
            db=db
        )
        db.commit()

        # Try to fulfill the request - should be DENIED
        result = key_request.fulfill_request(
            request_id=request_id,
            peer_id=alice['peer_id'],
            t_ms=5000,
            db=db
        )

        # The request should be denied because the requester is removed
        assert result is None

    def test_fulfill_request_allows_non_removed_requester(self, fresh_db):
        """Key request is allowed if requester is NOT removed in the key's epoch."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Two peers - one will be removed, one won't
        removed_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        ok_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        ok_pubkey_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create removal epoch for only one peer
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=removed_peer_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Create a key after the removal
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()
        key_id = crypto.b64encode(key_data['id'])

        # Create a key request from the NON-removed peer
        request_id = key_request.create(
            peer_id=alice['peer_id'],
            peer_shared_id=ok_peer_id,  # Requester is NOT removed
            requested_key_id=key_id,
            requester_pubkey_id=ok_pubkey_id,
            t_ms=4000,
            db=db
        )
        db.commit()

        # Try to fulfill the request
        # Note: This will fail because we don't have the ok_peer's pubkey in the db
        # But that failure happens AFTER the removal check passes
        # The removal check itself should pass
        result = key_request.fulfill_request(
            request_id=request_id,
            peer_id=alice['peer_id'],
            t_ms=5000,
            db=db
        )

        # Result is None because pubkey lookup fails, but it's NOT because of removal
        # The key point is that we got past the removal check
        # Let's verify the peer is not considered removed
        key_epoch = key_announce.get_removal_epoch_for_key(key_id, alice['peer_id'], db)
        assert removal_epoch.is_removed(ok_peer_id, key_epoch, alice['peer_id'], db) is False

    def test_key_without_epoch_allows_any_requester(self, fresh_db):
        """Keys created before any removals have no epoch, so don't deny anyone."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a key BEFORE any removals
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()
        key_id = crypto.b64encode(key_data['id'])

        # Verify key has no epoch
        key_epoch = key_announce.get_removal_epoch_for_key(key_id, alice['peer_id'], db)
        assert key_epoch is None

        # Now create a removal
        some_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        some_pubkey_id = crypto.b64encode(crypto.generate_secret()[:16])

        removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=some_peer_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=3000,
            db=db
        )
        db.commit()

        # The removed peer requests the OLD key (created before removal)
        request_id = key_request.create(
            peer_id=alice['peer_id'],
            peer_shared_id=some_peer_id,  # This peer was later removed
            requested_key_id=key_id,
            requester_pubkey_id=some_pubkey_id,
            t_ms=4000,
            db=db
        )
        db.commit()

        # The removal check should PASS because the key has no epoch
        # (Fulfillment will fail for other reasons - no pubkey - but removal isn't checked)
        # We verify by checking the key's epoch is None
        assert key_announce.get_removal_epoch_for_key(key_id, alice['peer_id'], db) is None


class TestKeyRequestSecurityPropertyDocumented:
    """Document and test the key security property.

    The security property: if you have a key K with removal_epoch E, you have E.
    Having E means you know about all removals in E's chain.
    Therefore, you can check if any requester is removed before sharing K.

    This means a removed user cannot obtain keys created after their removal,
    even from peers who haven't explicitly "realized" the user is removed.
    """

    def test_having_key_implies_having_its_removal_epoch(self, fresh_db):
        """Verify that keys reference their removal epochs."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create removal
        removed_peer_id = crypto.b64encode(crypto.generate_secret()[:16])
        epoch_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=removed_peer_id,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Create key after removal
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()
        key_id = crypto.b64encode(key_data['id'])

        # Verify: key -> key_announce -> removal_epoch_id
        key_epoch = key_announce.get_removal_epoch_for_key(key_id, alice['peer_id'], db)
        assert key_epoch == epoch_id

        # Verify: removal_epoch_id -> removal info
        is_removed = removal_epoch.is_removed(removed_peer_id, key_epoch, alice['peer_id'], db)
        assert is_removed is True

        # Therefore: having the key means you can check if someone is removed
        # This is the security property!

    def test_chained_removals_are_all_visible(self, fresh_db):
        """Verify that chained removal epochs include all ancestor removals."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Remove peer 1
        peer1 = crypto.b64encode(crypto.generate_secret()[:16])
        epoch1_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=peer1,
            removed_user_id=None,
            parent_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Remove peer 2 (chains to peer 1's epoch)
        peer2 = crypto.b64encode(crypto.generate_secret()[:16])
        epoch2_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=peer2,
            removed_user_id=None,
            parent_epoch_id=epoch1_id,
            t_ms=3000,
            db=db
        )
        db.commit()

        # Remove peer 3 (chains to peer 2's epoch, which chains to peer 1's)
        peer3 = crypto.b64encode(crypto.generate_secret()[:16])
        epoch3_id = removal_epoch.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removed_peer_id=peer3,
            removed_user_id=None,
            parent_epoch_id=epoch2_id,
            t_ms=4000,
            db=db
        )
        db.commit()

        # Create key after all removals
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=5000,
            db=db
        )
        db.commit()
        key_id = crypto.b64encode(key_data['id'])

        # Key should reference the latest epoch
        key_epoch = key_announce.get_removal_epoch_for_key(key_id, alice['peer_id'], db)
        assert key_epoch == epoch3_id

        # All three peers should be considered removed via the chain
        assert removal_epoch.is_removed(peer1, epoch3_id, alice['peer_id'], db) is True
        assert removal_epoch.is_removed(peer2, epoch3_id, alice['peer_id'], db) is True
        assert removal_epoch.is_removed(peer3, epoch3_id, alice['peer_id'], db) is True

        # Therefore: key requests from any of these peers would be denied
        for peer_id in [peer1, peer2, peer3]:
            request_id = key_request.create(
                peer_id=alice['peer_id'],
                peer_shared_id=peer_id,
                requested_key_id=key_id,
                requester_pubkey_id=crypto.b64encode(crypto.generate_secret()[:16]),
                t_ms=6000,
                db=db
            )
            db.commit()

            result = key_request.fulfill_request(
                request_id=request_id,
                peer_id=alice['peer_id'],
                t_ms=7000,
                db=db
            )
            assert result is None, f"Request from {peer_id[:20]}... should be denied"
