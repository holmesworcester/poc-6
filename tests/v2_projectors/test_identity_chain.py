"""V2 Projector Tests: Identity Event Chain

Tests the v2 projection of the identity event chain:
1. network - self-signed root of trust
2. invite (bootstrap) - signed by network
3. user - signed by invite
4. peer_shared - signed by invite (with fallback)
5. invite_accepted - local, stores trust anchor

Each test verifies:
- EVENT_SPEC is correctly configured
- project_pure() writes correct data
- Signer verification works
- Trust anchor mechanism (for network)
"""
import pytest
import sqlite3
import uuid
from pathlib import Path

from core.db import Database, create_safe_db, create_unsafe_db
from core import schema, store, crypto
from events.network import recorded
from events.identity import peer


def create_test_db():
    """Create fresh test database."""
    db_path = Path(f"/tmp/test_{uuid.uuid4().hex}.db")
    conn = sqlite3.connect(str(db_path))
    db = Database(conn)
    schema.create_all(db)
    return db


class TestNetworkProjector:
    """Tests for network event v2 projector."""

    def test_network_event_spec(self):
        """Verify network EVENT_SPEC is correctly configured."""
        from events.identity import network

        spec = network.EVENT_SPEC
        assert spec['encrypted'] is False
        assert spec['signer'] is not None
        assert spec['signer']['id_field'] == 'network_pubkey'
        assert spec['signer']['type_field'] == 'signer_type'

    def test_network_self_signed_validates(self):
        """Network events are self-signed and should validate via uniform trust anchor model.

        With uniform model, network only projects after invite_accepted inserts trust anchor.
        This test uses user.new_network() which handles the full bootstrap flow:
        1. Creates network event (stored, not yet projected)
        2. Creates invite_accepted (inserts trust anchor + projects network)
        3. Network is now valid
        """
        from events.identity import user

        db = create_test_db()

        # Use full bootstrap flow which handles trust anchor properly
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Check it was projected
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        networks = safedb.query_all(
            "SELECT network_id, network_pubkey FROM networks WHERE recorded_by = ?",
            (alice['peer_id'],)
        )
        assert len(networks) == 1
        assert networks[0]['network_id'] == alice['network_id']

        # Check it's valid
        valid = safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (alice['network_id'], alice['peer_id'])
        )
        assert valid is not None

    def test_network_validates_via_trust_anchor(self):
        """Network validates when it arrives if trust anchor exists."""
        db = create_test_db()
        bob_peer_id = peer.create(t_ms=1000, db=db)

        # Create a mock network event (as if Alice created it)
        network_private_key, network_public_key = crypto.generate_keypair()
        network_event_data = {
            'type': 'network',
            'network_pubkey': crypto.b64encode(network_public_key),
            'signer_type': 'network',
            'created_at': 500
        }
        signed_network = crypto.sign_event(network_event_data, network_private_key)
        network_blob = crypto.canonicalize_json(signed_network)
        network_id = crypto.b64encode(crypto.hash(network_blob))

        # Store network blob
        unsafedb = create_unsafe_db(db)
        store.blob(network_blob, 500, True, unsafedb)

        bob_safedb = create_safe_db(db, recorded_by=bob_peer_id)

        # Store trust anchor (simulating invite_accepted)
        bob_safedb.execute(
            "INSERT INTO trust_anchors (network_id, recorded_by, created_at) VALUES (?, ?, ?)",
            (network_id, bob_peer_id, 1000)
        )

        # Network should NOT be valid yet
        valid_before = bob_safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (network_id, bob_peer_id)
        )
        assert valid_before is None

        # Create recorded wrapper and project (simulating sync arrival)
        recorded_id = recorded.create(network_id, bob_peer_id, 2000, db, return_dupes=True)
        recorded.project(recorded_id, db)

        # Now network should be valid
        valid_after = bob_safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (network_id, bob_peer_id)
        )
        assert valid_after is not None

        # Networks table should have the entry
        networks = bob_safedb.query_all(
            "SELECT network_id FROM networks WHERE recorded_by = ?",
            (bob_peer_id,)
        )
        assert len(networks) == 1


class TestInviteProjector:
    """Tests for invite event v2 projector."""

    def test_invite_event_spec(self):
        """Verify invite EVENT_SPEC is correctly configured."""
        from events.identity import invite

        spec = invite.EVENT_SPEC
        assert spec['encrypted'] is False
        assert spec['signer'] is not None
        assert spec['signer']['type_field'] == 'signer_type'

    def test_bootstrap_invite_signed_by_network(self):
        """Bootstrap invite is signed by network and validates after network."""
        from events.identity import user

        db = create_test_db()

        # Alice creates network (includes bootstrap invite)
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        unsafedb = create_unsafe_db(db)

        # Find all invites for Alice
        invites = alice_safedb.query_all(
            "SELECT invite_id FROM invites WHERE recorded_by = ?",
            (alice['peer_id'],)
        )
        assert len(invites) >= 1, "Should have at least one invite"

        # Find bootstrap invite by checking blob's signed_by field
        bootstrap_invite_id = None
        for inv in invites:
            blob = store.get(inv['invite_id'], unsafedb)
            if blob:
                data = crypto.parse_json(blob)
                # Bootstrap invites are signed by network (signer_type='network')
                if data.get('signer_type') == 'network':
                    bootstrap_invite_id = inv['invite_id']
                    break

        assert bootstrap_invite_id is not None, "Bootstrap invite should exist"

        # Verify it's valid
        valid = alice_safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (bootstrap_invite_id, alice['peer_id'])
        )
        assert valid is not None, "Bootstrap invite should be valid"


class TestUserProjector:
    """Tests for user event v2 projector."""

    def test_user_event_spec(self):
        """Verify user EVENT_SPEC is correctly configured."""
        from events.identity import user

        spec = user.EVENT_SPEC
        assert spec['encrypted'] is False
        assert spec['signer'] is not None
        assert spec['signer']['type_field'] == 'signer_type'
        assert spec['signer']['id_field'] == 'signed_by'

    def test_user_signed_by_invite(self):
        """User event is signed by invite."""
        from events.identity import user

        db = create_test_db()
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])

        # Get user event
        users = alice_safedb.query_all(
            "SELECT user_id, network_id FROM users WHERE recorded_by = ?",
            (alice['peer_id'],)
        )
        assert len(users) >= 1

        # User should be valid
        valid = alice_safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (users[0]['user_id'], alice['peer_id'])
        )
        assert valid is not None


class TestPeerSharedProjector:
    """Tests for peer_shared event v2 projector."""

    def test_peer_shared_event_spec(self):
        """Verify peer_shared EVENT_SPEC is correctly configured."""
        from events.identity import peer_shared

        spec = peer_shared.EVENT_SPEC
        assert spec['encrypted'] is False
        # peer_shared has signer: None due to complex bootstrap verification
        assert spec['signer'] is None

    def test_peer_shared_projects(self):
        """peer_shared event projects correctly."""
        from events.identity import user

        db = create_test_db()
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])

        # Check peer_shared table
        peers_shared = alice_safedb.query_all(
            "SELECT peer_shared_id, user_id FROM peers_shared WHERE recorded_by = ?",
            (alice['peer_id'],)
        )
        assert len(peers_shared) >= 1
        assert peers_shared[0]['peer_shared_id'] == alice['peer_shared_id']


class TestInviteAcceptedProjector:
    """Tests for invite_accepted event v2 projector."""

    def test_invite_accepted_event_spec(self):
        """Verify invite_accepted EVENT_SPEC is correctly configured."""
        from events.identity import invite_accepted

        spec = invite_accepted.EVENT_SPEC
        assert spec['encrypted'] is False
        assert spec['signer'] is None  # Local-only, unsigned

    def test_invite_accepted_stores_trust_anchor(self):
        """invite_accepted stores network_id in trust_anchors."""
        from events.identity import user, invite

        db = create_test_db()

        # Alice creates network and invite
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
        db.commit()

        # Bob joins
        bob_peer_id = peer.create(t_ms=2000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
        db.commit()

        bob_safedb = create_safe_db(db, recorded_by=bob_peer_id)

        # Check trust_anchors table
        trust_anchors = bob_safedb.query_all(
            "SELECT network_id FROM trust_anchors WHERE recorded_by = ?",
            (bob_peer_id,)
        )
        assert len(trust_anchors) == 1
        assert trust_anchors[0]['network_id'] == alice['network_id']

        # With uniform model: network projects when invite_accepted runs (if blob available)
        # In this test, Alice and Bob share a db, so the network blob is available
        valid = bob_safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (alice['network_id'], bob_peer_id)
        )
        assert valid is not None, "Network should be valid (blob available, trust anchor exists)"


class TestIdentityChainCascade:
    """Tests for the full identity chain cascade."""

    def test_single_player_chain_validates(self):
        """Single player: full chain validates in order."""
        from events.identity import user

        db = create_test_db()
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])

        # All key events should be valid
        key_events = [
            ('network', alice['network_id']),
            ('user', alice['user_id']),
            ('peer_shared', alice['peer_shared_id']),
            ('channel', alice['channel_id']),
        ]

        for event_type, event_id in key_events:
            valid = alice_safedb.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
                (event_id, alice['peer_id'])
            )
            assert valid is not None, f"{event_type} should be valid"

    def test_network_cascade_unblocks_dependents(self):
        """When network validates, it should unblock events waiting on it."""
        from events.network import recorded
        from core import queues

        db = create_test_db()
        bob_peer_id = peer.create(t_ms=1000, db=db)
        bob_safedb = create_safe_db(db, recorded_by=bob_peer_id)
        unsafedb = create_unsafe_db(db)

        # Create mock network
        network_private_key, network_public_key = crypto.generate_keypair()
        network_event_data = {
            'type': 'network',
            'network_pubkey': crypto.b64encode(network_public_key),
            'signer_type': 'network',
            'created_at': 500
        }
        signed_network = crypto.sign_event(network_event_data, network_private_key)
        network_blob = crypto.canonicalize_json(signed_network)
        network_id = crypto.b64encode(crypto.hash(network_blob))
        store.blob(network_blob, 500, True, unsafedb)

        # Create mock bootstrap invite (signed by network)
        invite_private_key, invite_public_key = crypto.generate_keypair()
        invite_event_data = {
            'type': 'invite',
            'invite_pubkey': crypto.b64encode(invite_public_key),
            'group_id': 'mock_group',
            'mode': 'user',
            'signed_by': network_id,
            'signer_type': 'network',
            'created_at': 600
        }
        signed_invite = crypto.sign_event(invite_event_data, network_private_key)
        invite_blob = crypto.canonicalize_json(signed_invite)
        invite_id = crypto.b64encode(crypto.hash(invite_blob))
        store.blob(invite_blob, 600, True, unsafedb)

        # Store trust anchor
        bob_safedb.execute(
            "INSERT INTO trust_anchors (network_id, recorded_by, created_at) VALUES (?, ?, ?)",
            (network_id, bob_peer_id, 1000)
        )

        # Try to project invite - should block on network
        invite_recorded_id = recorded.create(invite_id, bob_peer_id, 1100, db, return_dupes=True)
        recorded.project(invite_recorded_id, db)

        # Invite should be blocked
        blocked = bob_safedb.query_one(
            "SELECT 1 FROM blocked_events_ephemeral WHERE recorded_by = ?",
            (bob_peer_id,)
        )
        assert blocked is not None, "Invite should be blocked waiting for network"

        # Now project network - should cascade and unblock invite
        network_recorded_id = recorded.create(network_id, bob_peer_id, 1200, db, return_dupes=True)
        recorded.project(network_recorded_id, db)

        # Network should be valid
        network_valid = bob_safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (network_id, bob_peer_id)
        )
        assert network_valid is not None, "Network should be valid"

        # Invite should now be valid (unblocked by network cascade)
        invite_valid = bob_safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (invite_id, bob_peer_id)
        )
        assert invite_valid is not None, "Invite should be valid after network cascade"

    def test_dependency_chain_documented(self):
        """Document the expected dependency chain for the identity flow.

        The chain from trust anchor to connection:
        1. invite_accepted (local) → stores network_id in trust_anchors
        2. network (sync) → validates via trust anchor, self-signed
        3. user_invite (sync) → signed by network, depends on network
        4. user (sync) → signed by user_invite, depends on user_invite
        5. peer_invite (sync) → signed by user, depends on user
        6. peer_shared (sync) → signed by peer_invite, depends on peer_invite
        7. ongoing_invite (sync) → signed by peer_shared, depends on peer_shared
        8. Bob's user (local but blocked) → signed by ongoing_invite, depends on ongoing_invite
        9. connection → established when both peers have peer_shared

        Each event's signer must be valid before the event can validate.
        Bob's local events BLOCK until the cascade from network reaches them via sync.
        """
        from events.identity import user, invite

        db = create_test_db()

        # Alice creates full network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
        db.commit()

        # Bob joins - his events that depend on Alice's cascade will BLOCK
        bob_peer_id = peer.create(t_ms=2000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
        db.commit()

        alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        bob_safedb = create_safe_db(db, recorded_by=bob_peer_id)

        # Document: Alice's chain is complete (she created everything locally)
        alice_chain = [
            ('network', alice['network_id']),
            ('user', alice['user_id']),
            ('peer_shared', alice['peer_shared_id']),
            ('channel', alice['channel_id']),
        ]
        for event_type, event_id in alice_chain:
            valid = alice_safedb.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
                (event_id, alice['peer_id'])
            )
            assert valid is not None, f"Alice's {event_type} should be valid"

        # Document: Bob has trust anchor for Alice's network
        trust_anchor = bob_safedb.query_one(
            "SELECT network_id FROM trust_anchors WHERE recorded_by = ?",
            (bob_peer_id,)
        )
        assert trust_anchor is not None, "Bob should have trust anchor"
        assert trust_anchor['network_id'] == alice['network_id'], "Trust anchor should be Alice's network"

        # Document: With uniform model, Alice's network IS valid for Bob when blob is available
        # In this test, Alice and Bob share a db, so invite_accepted projects network immediately
        alice_network_valid_for_bob = bob_safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (alice['network_id'], bob_peer_id)
        )
        assert alice_network_valid_for_bob is not None, (
            "Alice's network should be valid for Bob (blob available + trust anchor exists). "
            "In a real multi-device scenario, network would project when blob arrives via sync."
        )

        # Document: With network valid, Bob's events may still be blocked waiting on the rest of the cascade
        # (invite → user → peer_shared, etc.) unless those blobs are also available
        # In this shared-db test, some events may unblock immediately
        blocked = db.query_all(
            "SELECT recorded_id FROM blocked_events_ephemeral WHERE recorded_by = ?",
            (bob_peer_id,)
        )
        # Some events may still be blocked (waiting for cascade from invite → user → peer_shared)
        # but network should have unblocked events depending only on it
