"""
Connection Limiting Tests

Tests that connection limiting (k=N) works correctly for sync:
- Each peer only syncs with at most k connections
- Messages still propagate through multi-hop graph
- Deterministic connection selection via key_id sort
"""

import pytest
from core import tick
from core import transport
from core.db import create_safe_db
from events.identity import user, invite, peer
from events.content import message
from tests.utils.tick_helper import run_ticks, assert_eventually


class TestConnectionLimiting:
    """Tests for connection limiting feature."""

    def test_connection_limit_api(self, fresh_db):
        """Test set/get/reset of connection limit."""
        # Default is None (unlimited)
        assert transport.get_max_sync_connections() is None

        # Set limit
        transport.set_max_sync_connections(5)
        assert transport.get_max_sync_connections() == 5

        # Change limit
        transport.set_max_sync_connections(20)
        assert transport.get_max_sync_connections() == 20

        # Clear limit
        transport.set_max_sync_connections(None)
        assert transport.get_max_sync_connections() is None

    def test_connection_limit_reset_on_transport_reset(self, fresh_db):
        """Connection limit is cleared when transport is reset."""
        transport.set_max_sync_connections(10)
        assert transport.get_max_sync_connections() == 10

        transport.reset()
        assert transport.get_max_sync_connections() is None

        # Re-enable loopback for other tests
        transport.enable_loopback()

    def test_sync_with_connection_limit_small(self, fresh_db):
        """Test sync works with small connection limit (k=3) on 5 peers.

        Connection limiting is enabled FROM THE START (realistic scenario).
        """
        db = fresh_db
        tick.reset_state(db)

        # Enable connection limiting from the start (realistic)
        transport.set_max_sync_connections(3)

        t_ms = 1000

        # Create network with Alice
        alice = user.new_network(name='Alice', t_ms=t_ms, db=db)
        db.commit()
        t_ms += 100

        peers = [alice]

        # Create 4 more peers (5 total)
        for i in range(1, 5):
            invite_id, invite_link, _ = invite.create(
                peer_id=alice['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            new_peer_id = peer.create(t_ms=t_ms, db=db)
            new_user = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'User{i}',
                t_ms=t_ms,
                db=db
            )
            peers.append(new_user)
            t_ms += 10

        db.commit()

        # Sync with connection limiting active
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=100)

        # Alice sends a message
        channel_id = alice['channel_id']
        msg_result = message.create(
            channel_id=channel_id,
            content='Hello with connection limit!',
            peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )
        msg_id = msg_result['id']
        db.commit()
        t_ms += 100

        # Verify message reaches all peers (may need multi-hop)
        def all_peers_have_message():
            for p in peers:
                safedb = create_safe_db(db, recorded_by=p['peer_id'])
                row = safedb.query_one(
                    "SELECT message_id FROM messages WHERE message_id = ? AND recorded_by = ?",
                    (msg_id, p['peer_id'])
                )
                if not row:
                    return False
            return True

        # With k=3 and 5 peers, ring diameter ~2 hops
        assert_eventually(
            all_peers_have_message,
            db=db,
            start_t_ms=t_ms,
            max_rounds=200,
            msg="Message did not reach all 5 peers with k=3 limit"
        )

    def test_sync_with_connection_limit_medium(self, fresh_db):
        """Test sync works with medium connection limit (k=5) on 20 peers.

        Connection limiting is enabled FROM THE START (realistic scenario).
        The ring topology ensures the graph is connected despite sparse edges.
        """
        db = fresh_db
        tick.reset_state(db)

        # Enable connection limiting from the start (realistic)
        transport.set_max_sync_connections(5)

        t_ms = 1000

        # Create network with Alice
        alice = user.new_network(name='Alice', t_ms=t_ms, db=db)
        db.commit()
        t_ms += 100

        peers = [alice]

        # Create 19 more peers (20 total)
        for i in range(1, 20):
            invite_id, invite_link, _ = invite.create(
                peer_id=alice['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            new_peer_id = peer.create(t_ms=t_ms, db=db)
            new_user = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'User{i}',
                t_ms=t_ms,
                db=db
            )
            peers.append(new_user)
            t_ms += 10

        db.commit()

        # Sync with connection limiting active
        # Ring topology with k=5 gives diameter ~4 (20/5), need more rounds for multi-hop
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=300)

        # Alice sends a message
        channel_id = alice['channel_id']
        msg_result = message.create(
            channel_id=channel_id,
            content='Hello with k=5 connection limit!',
            peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )
        msg_id = msg_result['id']
        db.commit()
        t_ms += 100

        # Verify message reaches all peers
        def all_peers_have_message():
            for p in peers:
                safedb = create_safe_db(db, recorded_by=p['peer_id'])
                row = safedb.query_one(
                    "SELECT message_id FROM messages WHERE message_id = ? AND recorded_by = ?",
                    (msg_id, p['peer_id'])
                )
                if not row:
                    return False
            return True

        # With k=5 on 20 peers, ring diameter ~4 hops
        # Need enough rounds for multi-hop propagation
        assert_eventually(
            all_peers_have_message,
            db=db,
            start_t_ms=t_ms,
            max_rounds=500,
            msg="Message did not reach all 20 peers with k=5 limit"
        )

    def test_unlimited_connections_still_works(self, fresh_db):
        """Test that unlimited connections (k=None) still works normally."""
        db = fresh_db
        tick.reset_state(db)

        # Explicitly set unlimited (should be default but be explicit)
        transport.set_max_sync_connections(None)

        t_ms = 1000

        # Create network with Alice
        alice = user.new_network(name='Alice', t_ms=t_ms, db=db)
        db.commit()
        t_ms += 100

        peers = [alice]

        # Create 4 more peers
        for i in range(1, 5):
            invite_id, invite_link, _ = invite.create(
                peer_id=alice['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            new_peer_id = peer.create(t_ms=t_ms, db=db)
            new_user = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'User{i}',
                t_ms=t_ms,
                db=db
            )
            peers.append(new_user)
            t_ms += 10

        db.commit()

        # Sync (full mesh - everyone syncs with everyone)
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=50)

        # Alice sends a message
        channel_id = alice['channel_id']
        msg_result = message.create(
            channel_id=channel_id,
            content='Hello with unlimited connections!',
            peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )
        msg_id = msg_result['id']
        db.commit()
        t_ms += 100

        # Verify message reaches all peers quickly (single hop in full mesh)
        def all_peers_have_message():
            for p in peers:
                safedb = create_safe_db(db, recorded_by=p['peer_id'])
                row = safedb.query_one(
                    "SELECT message_id FROM messages WHERE message_id = ? AND recorded_by = ?",
                    (msg_id, p['peer_id'])
                )
                if not row:
                    return False
            return True

        assert_eventually(
            all_peers_have_message,
            db=db,
            start_t_ms=t_ms,
            max_rounds=50,  # Should be fast with full mesh
            msg="Message did not reach all 5 peers with unlimited connections"
        )
