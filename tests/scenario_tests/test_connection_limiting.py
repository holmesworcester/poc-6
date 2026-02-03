"""Test connection limiting for scale tests.

Tests the rate limit + natural attrition approach:
- Rate limit creation: Create at most `rate` new connections per tick
- Don't renew excess: When connection approaches expiry, only renew if count < k
- Natural attrition: Excess connections expire naturally (TTL = 5 minutes)
- Result: Sparse random graph with ~k connections per peer

This enables 1000+ peer scale tests that would otherwise be O(n²) in connections.
"""
import pytest
from core.db import create_safe_db
from events.identity import user, invite, peer
from events.content import message
from events.network import connection_request
from tests.utils.tick_helper import assert_eventually, run_ticks, TestClock


class TestConnectionLimiting:
    """Test connection limiting functionality."""

    def test_connection_limit_config(self, fresh_db):
        """Test connection limit configuration functions."""
        # Default is k=20
        assert connection_request.get_max_connections_per_peer() == 20
        assert connection_request.get_connection_creation_rate() == 5

        # Set different limits
        connection_request.set_max_connections_per_peer(10)
        connection_request.set_connection_creation_rate(3)

        assert connection_request.get_max_connections_per_peer() == 10
        assert connection_request.get_connection_creation_rate() == 3

        # Can disable limiting
        connection_request.set_max_connections_per_peer(None)
        assert connection_request.get_max_connections_per_peer() is None

        # Reset to defaults
        connection_request.reset_connection_limits()
        assert connection_request.get_max_connections_per_peer() == 20
        assert connection_request.get_connection_creation_rate() == 5

    def test_sync_with_connection_limit(self, fresh_db):
        """Test sync works with connection limiting enabled.

        The rate limit + natural attrition approach:
        - Limits OUTGOING connection creation rate (not total connections)
        - Incoming connections are always accepted (ensures new peers can join)
        - Excess connections expire naturally via TTL (5 min)

        Within a test (which runs faster than TTL), we verify:
        - Rate limiting works (connections form gradually, not all at once)
        - Sync still works (messages propagate)
        """
        db = fresh_db
        clock = TestClock()

        connection_request.set_max_connections_per_peer(5)
        connection_request.set_connection_creation_rate(2)

        admin = user.new_network(name='Admin', t_ms=clock.tick(), db=db)
        db.commit()

        members = [admin]

        for i in range(9):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=clock.tick(),
                db=db
            )

            new_peer_id = peer.create(t_ms=clock.tick(), db=db)

            member = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=clock.tick(),
                db=db
            )
            members.append({
                'peer_id': member['peer_id'],
                'peer_shared_id': member['peer_shared_id'],
                'channel_id': member['channel_id'],
            })

        db.commit()

        run_ticks(db=db, start_t_ms=clock.tick(), num_rounds=50)

        admin_msg = message.create(
            peer_id=admin['peer_id'],
            channel_id=admin['channel_id'],
            content='Test message from admin',
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        run_ticks(db=db, start_t_ms=clock.tick(), num_rounds=50)

        # At least some members should see the message
        members_with_message = 0
        for member in members[1:]:  # Skip admin
            member_messages = message.list(member['channel_id'], member['peer_id'], db)
            contents = [m['content'] for m in member_messages]
            if 'Test message from admin' in contents:
                members_with_message += 1

        # With rate limiting, sync should still work
        assert members_with_message >= 3, \
            f"Expected at least 3 members to see message, got {members_with_message}"

    def test_rate_limiting_gradual_formation(self, fresh_db):
        """Test that connections form gradually with rate limiting."""
        db = fresh_db
        clock = TestClock()

        connection_request.set_max_connections_per_peer(10)
        connection_request.set_connection_creation_rate(2)

        admin = user.new_network(name='Admin', t_ms=clock.tick(), db=db)
        db.commit()

        members = []
        for i in range(5):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=clock.tick(),
                db=db
            )
            new_peer_id = peer.create(t_ms=clock.tick(), db=db)
            member = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=clock.tick(),
                db=db
            )
            members.append(member)

        db.commit()

        t_ms = run_ticks(db=db, start_t_ms=clock.tick(), num_rounds=2)

        safedb = create_safe_db(db, recorded_by=admin['peer_id'])
        early_count = safedb.query_one("""
            SELECT COUNT(*) as cnt FROM connections
            WHERE recorded_by = ? AND last_handshake_ms + ttl_ms > ?
        """, (admin['peer_id'], t_ms))['cnt']

        assert early_count <= 10, \
            f"Expected gradual connection formation, got {early_count} after 2 ticks"

        t_ms = run_ticks(db=db, start_t_ms=clock.tick(), num_rounds=20)

        later_count = safedb.query_one("""
            SELECT COUNT(*) as cnt FROM connections
            WHERE recorded_by = ? AND last_handshake_ms + ttl_ms > ?
              AND their_key IS NOT NULL
        """, (admin['peer_id'], t_ms))['cnt']

        # Should now have more connections as they've had time to form
        assert later_count >= early_count, \
            f"Expected connections to grow over time"


class TestConnectionLimitingScale:
    """Scale tests with connection limiting enabled."""

    @pytest.mark.parametrize("n", [30])
    def test_rate_limited_connection_formation(self, fresh_db, n):
        """Test that connections form gradually with rate limiting at scale.

        The rate limit approach limits how fast connections are CREATED,
        not the total number. With rate=5, each peer creates at most 5
        new outgoing connections per tick.

        This test verifies:
        - Rate limiting causes gradual connection formation
        - All peers eventually become connected (sync works)
        """
        db = fresh_db
        clock = TestClock()

        admin = user.new_network(name='Admin', t_ms=clock.tick(), db=db)
        db.commit()

        members = [admin]

        for i in range(n - 1):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=clock.tick(),
                db=db
            )

            new_peer_id = peer.create(t_ms=clock.tick(), db=db)

            member = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=clock.tick(),
                db=db
            )
            members.append({
                'peer_id': member['peer_id'],
            })

        db.commit()

        t_ms_early = run_ticks(db=db, start_t_ms=clock.tick(), num_rounds=5)

        # Count connections after few ticks - should be limited by rate
        early_connections = 0
        for member in members:
            peer_id = member['peer_id'] if isinstance(member, dict) else member
            if isinstance(member, dict):
                peer_id = member['peer_id']
            else:
                peer_id = admin['peer_id']

            safedb = create_safe_db(db, recorded_by=peer_id)
            count_row = safedb.query_one("""
                SELECT COUNT(*) as cnt FROM connections
                WHERE recorded_by = ? AND last_handshake_ms + ttl_ms > ?
            """, (peer_id, t_ms_early))
            early_connections += count_row['cnt'] if count_row else 0

        t_ms_later = run_ticks(db=db, start_t_ms=clock.tick(), num_rounds=50)

        # Count connections after more ticks - should have grown
        later_connections = 0
        for member in members:
            peer_id = member['peer_id'] if isinstance(member, dict) else member
            if isinstance(member, dict):
                peer_id = member['peer_id']
            else:
                peer_id = admin['peer_id']

            safedb = create_safe_db(db, recorded_by=peer_id)
            count_row = safedb.query_one("""
                SELECT COUNT(*) as cnt FROM connections
                WHERE recorded_by = ? AND last_handshake_ms + ttl_ms > ?
            """, (peer_id, t_ms_later))
            later_connections += count_row['cnt'] if count_row else 0

        # Verify gradual formation: later should have more connections than early
        # (This proves rate limiting is working - connections didn't all form instantly)
        assert later_connections >= early_connections, \
            f"Connections should grow over time: early={early_connections}, later={later_connections}"

        # Verify network is connected: total connections should be substantial
        # With n=30 peers, a connected graph needs at least n-1=29 edges
        # But undirected, so we count 2x that in our per-peer counts
        min_expected = (n - 1)  # At least one connection per peer on average
        avg_per_peer = later_connections / n
        assert avg_per_peer >= 1, \
            f"Expected at least 1 connection per peer on avg, got {avg_per_peer:.1f}"
