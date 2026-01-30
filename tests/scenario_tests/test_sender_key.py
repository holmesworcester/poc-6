"""
Sender Key Tests: Verify senders pick and distribute their own keys.

Tests the sender key model where:
- Each sender creates their own symmetric key per group
- Sender distributes their key to recipients via TreeKEM copath
- Messages are encrypted with the sender's key
- Recipients decrypt using the sender's key they received
"""

import pytest
from core import crypto
from events.identity import user, peer
from events.group import sender_key, secret, pubkey, treekem_pubkey, key_announce


class TestSenderKeySelection:
    """Test that senders pick the correct key for their messages."""

    def test_first_message_creates_new_key(self, fresh_db):
        """First message from sender should create a new key."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # First message should create a new key
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        assert key_data is not None
        assert key_data['type'] == 'symmetric'
        assert len(key_data['key']) == 32

    def test_subsequent_message_reuses_key(self, fresh_db):
        """Subsequent messages from same sender should reuse existing key."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create a test group_id
        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # First message creates key
        key_data_1 = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        key_id_1 = crypto.b64encode(key_data_1['id'])

        # Second message should reuse the same key
        key_data_2 = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        key_id_2 = crypto.b64encode(key_data_2['id'])

        # Should be the same key
        assert key_id_1 == key_id_2
        assert key_data_1['key'] == key_data_2['key']

    def test_different_groups_get_different_keys(self, fresh_db):
        """Same sender in different groups should have different keys."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create two different groups
        group_1 = crypto.b64encode(crypto.generate_secret()[:16])
        group_2 = crypto.b64encode(crypto.generate_secret()[:16])

        # Get key for group 1
        key_data_1 = sender_key.pick_or_create_key(
            group_id=group_1,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        # Get key for group 2
        key_data_2 = sender_key.pick_or_create_key(
            group_id=group_2,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        # Should be different keys
        key_id_1 = crypto.b64encode(key_data_1['id'])
        key_id_2 = crypto.b64encode(key_data_2['id'])

        assert key_id_1 != key_id_2

    def test_key_is_announced(self, fresh_db):
        """Creating a sender key should also create a key_announce event."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create key
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        key_id = crypto.b64encode(key_data['id'])

        # Check key was announced
        announces = key_announce.get_announced_keys_for_group(
            test_group_id, alice['peer_id'], db
        )

        assert len(announces) >= 1
        assert any(a['key_id'] == key_id for a in announces)


class TestSenderKeyDistribution:
    """Test that sender keys are distributed correctly."""

    def test_key_distributed_via_leaf_fallback(self, fresh_db):
        """When no copath pubkeys exist, fall back to leaf distribution."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create a secret manually (without distribution)
        secret_id = secret.create(
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            group_id=test_group_id,
            removal_epoch_id=None,
            t_ms=2000,
            db=db
        )
        db.commit()

        # Distribute should fall back to leaf (no copath pubkeys)
        shared_ids = sender_key.distribute_key_to_group(
            secret_id=secret_id,
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            removal_epoch_id=None,
            t_ms=3000,
            db=db
        )
        db.commit()

        # With only one peer (Alice), should be empty (no one else to distribute to)
        assert shared_ids == []

    def test_key_distributed_to_copath_pubkeys(self, fresh_db):
        """When copath pubkeys exist, distribute to them."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

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

        # Create copath pubkeys at different positions
        # These simulate other peers having published their treekem_pubkeys
        copath_pubkey_ids = []
        for i, prefix in enumerate([b'\x80', b'\x40']):
            pk_id, _, _ = treekem_pubkey.create(
                depth=1,
                path_prefix=prefix,
                parent_pubkey_shared_id=None,
                removal_epoch_id=None,
                peer_id=alice['peer_id'],
                peer_shared_id=alice['peer_shared_id'],
                t_ms=2500 + i,
                db=db
            )
            copath_pubkey_ids.append(pk_id)
        db.commit()

        # Now distribute using the copath
        from events.group import secret_shared
        shared_ids = secret_shared.share_secret_with_pubkeys(
            secret_id=secret_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            recipient_pubkey_ids=copath_pubkey_ids,
            removal_epoch_id=None,
            t_ms=3000,
            db=db
        )
        db.commit()

        # Should have distributed to both copath pubkeys
        assert len(shared_ids) == 2


class TestSenderKeyLookup:
    """Test looking up sender keys for decryption."""

    def test_get_sender_key_for_group(self, fresh_db):
        """Can look up a sender's key for a group."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create key
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        expected_key_id = crypto.b64encode(key_data['id'])

        # Look up the key
        found_key_id = sender_key.get_sender_key_for_group(
            group_id=test_group_id,
            sender_peer_shared_id=alice['peer_shared_id'],
            recorded_by=alice['peer_id'],
            db=db
        )

        assert found_key_id == expected_key_id

    def test_get_key_for_decryption(self, fresh_db):
        """Can get a key by ID for decryption."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Create key
        key_data = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        key_id = crypto.b64encode(key_data['id'])

        # Get key for decryption
        decryption_key = sender_key.get_key_for_decryption(
            key_id=key_id,
            recorded_by=alice['peer_id'],
            db=db
        )

        assert decryption_key is not None
        assert decryption_key['key'] == key_data['key']

    def test_missing_key_returns_none(self, fresh_db):
        """Looking up a non-existent key returns None."""
        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        fake_key_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Should return None for non-existent key
        result = sender_key.get_key_for_decryption(
            key_id=fake_key_id,
            recorded_by=alice['peer_id'],
            db=db
        )

        assert result is None


class TestSenderKeyIsolation:
    """Test that sender keys are properly isolated between senders."""

    def test_different_senders_different_keys(self, fresh_db):
        """Different senders in same group should have different keys."""
        from events.identity import invite
        from tests.utils.tick_helper import run_ticks

        db = fresh_db
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Create Bob as a real user by having him join Alice's network
        _, invite_link, _ = invite.create(
            peer_id=alice['peer_id'],
            t_ms=1100,
            db=db
        )
        db.commit()

        bob_peer_id = peer.create(t_ms=1200, db=db)
        bob = user.join(
            peer_id=bob_peer_id,
            invite_link=invite_link,
            name='Bob',
            t_ms=1200,
            db=db
        )
        db.commit()

        # Run sync ticks to converge state between Alice and Bob's views
        run_ticks(db=db, start_t_ms=None, num_rounds=15)

        test_group_id = crypto.b64encode(crypto.generate_secret()[:16])

        # Alice creates her key (using her own context)
        alice_key = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=alice['peer_id'],
            peer_shared_id=alice['peer_shared_id'],
            t_ms=2000,
            db=db
        )
        db.commit()

        alice_key_id = crypto.b64encode(alice_key['id'])

        # Bob creates his key (using Bob's context)
        bob_key = sender_key.pick_or_create_key(
            group_id=test_group_id,
            peer_id=bob['peer_id'],
            peer_shared_id=bob['peer_shared_id'],
            t_ms=3000,
            db=db
        )
        db.commit()

        bob_key_id = crypto.b64encode(bob_key['id'])

        # Keys should be different
        assert alice_key_id != bob_key_id

        # Looking up Alice's key (from Alice's view) should find Alice's key
        found_alice_key = sender_key.get_sender_key_for_group(
            group_id=test_group_id,
            sender_peer_shared_id=alice['peer_shared_id'],
            recorded_by=alice['peer_id'],
            db=db
        )
        assert found_alice_key == alice_key_id

        # Looking up Bob's key (from Bob's view) should find Bob's key
        found_bob_key = sender_key.get_sender_key_for_group(
            group_id=test_group_id,
            sender_peer_shared_id=bob['peer_shared_id'],
            recorded_by=bob['peer_id'],
            db=db
        )
        assert found_bob_key == bob_key_id
