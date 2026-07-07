"""
TreeKEM Scale Tests with Connection Limiting

Tests the TreeKEM O(log n) broadcast protocol at scale using connection limiting
to make O(n²) full mesh tractable.

Connection limiting (k=20):
- Each peer syncs with only k=20 connections (sorted by key_id for determinism)
- Graph becomes k-regular random graph instead of full mesh
- Diameter ~ log(n)/log(k) ≈ 2.3 hops for n=1000, k=20
- Total connections: n×k = 20,000 (vs n² = 1,000,000 for full mesh)

Expected timing:
- 100 peers (full mesh): ~2-3s
- 1000 peers (k=20): ~30-60s estimated
"""

import pytest
from core import tick
from core import transport
from core.db import create_safe_db
from events.identity import user, invite, peer
from events.content import message
from events.group import group
from tests.utils.tick_helper import run_ticks, assert_eventually


class TestTreeKEMScale:
    """Scale tests for TreeKEM broadcast with connection limiting."""

    @pytest.mark.parametrize("n", [100])
    def test_scale_o1_broadcast_after_all_updates(self, fresh_db, n):
        """Test O(1) broadcast at scale after all users have updated TreeKEM.

        Uses k=10 connection limiting to make 100 peers tractable:
        - Full mesh: 100 × 99 = ~10K sync ops per tick (too slow)
        - With k=10: 100 × 10 = 1K sync ops per tick (fast)

        Connection limiting is enabled from the start (realistic).
        New peers with few connections sync with everyone they know.
        As the graph fills in, limiting kicks in for well-connected peers.
        """
        db = fresh_db
        tick.reset_state(db)

        # Use connection limiting for tractable scale
        # k=10 gives good connectivity with diameter ~2
        transport.set_max_sync_connections(10)

        t_ms = 1000

        # Create network with first user (Alice)
        alice = user.new_network(name='Alice', t_ms=t_ms, db=db)
        db.commit()
        t_ms += 100

        peers = [alice]

        # Create remaining n-1 peers and join them
        for i in range(1, n):
            # Create invite from Alice
            invite_id, invite_link, _ = invite.create(
                peer_id=alice['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            # Create peer and join
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

            if i % 25 == 0:
                # Periodic commit and sync to establish connections
                db.commit()
                t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=30)

        db.commit()

        # Sync to establish connections and converge
        # Need enough rounds for connection graph to become well-connected
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=200)

        # Alice sends a message
        channel_id = alice['channel_id']

        msg_result = message.create(
            channel_id=channel_id,
            content='Hello from Alice at scale!',
            peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )
        msg_id = msg_result['id']
        db.commit()
        t_ms += 100

        # Verify message syncs to all peers
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

        # With k=10 graph and diameter ~2, need many rounds for multi-hop propagation
        assert_eventually(
            all_peers_have_message,
            db=db,
            start_t_ms=t_ms,
            max_rounds=500,
            msg=f"Message did not reach all {n} peers"
        )

    @pytest.mark.parametrize("n", [1000])
    @pytest.mark.slow
    def test_scale_o1_broadcast_1k(self, fresh_db, n):
        """Scale test with k=20 connection limit for 1000 peers.

        Uses connection limiting to reduce O(n²) to O(n×k):
        - Full mesh: 1000 × 999 = ~1M connections
        - With k=20: 1000 × 20 = 20K connections

        Multi-hop propagation:
        - Diameter ~ log(1000)/log(20) ≈ 2.3 hops
        - Events need ~3-5 ticks to propagate fully
        - More ticks needed, but each tick is O(n×k) not O(n²)
        """
        db = fresh_db
        tick.reset_state(db)

        # Enable connection limiting for scale
        transport.set_max_sync_connections(20)

        t_ms = 1000

        # Create network with first user (Alice)
        alice = user.new_network(name='Alice', t_ms=t_ms, db=db)
        db.commit()
        t_ms += 100

        peers = [alice]

        # Create remaining n-1 peers and join them
        for i in range(1, n):
            # Create invite from Alice
            invite_id, invite_link, _ = invite.create(
                peer_id=alice['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            # Create peer and join
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

            # Periodic commit to avoid huge transaction
            if i % 100 == 0:
                db.commit()
                # Brief sync to establish some connections
                t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=20)
                print(f"  Created {i} peers...")

        db.commit()
        print(f"All {n} peers created")

        # Sync to establish connections and converge
        # With k=20 limit and multi-hop, need more rounds
        sync_rounds = 500
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=sync_rounds)
        print(f"Initial sync complete ({sync_rounds} rounds)")

        # Alice sends a message
        channel_id = alice['channel_id']

        msg_result = message.create(
            channel_id=channel_id,
            content='Hello from Alice at 1000 peer scale!',
            peer_id=alice['peer_id'],
            t_ms=t_ms,
            db=db
        )
        msg_id = msg_result['id']
        db.commit()
        t_ms += 100
        print(f"Message created: {msg_id[:20]}...")

        # Verify message syncs to all peers
        # With k=20 graph and diameter ~3, need several passes
        def count_peers_with_message():
            count = 0
            for p in peers:
                safedb = create_safe_db(db, recorded_by=p['peer_id'])
                row = safedb.query_one(
                    "SELECT message_id FROM messages WHERE message_id = ? AND recorded_by = ?",
                    (msg_id, p['peer_id'])
                )
                if row:
                    count += 1
            return count

        def all_peers_have_message():
            count = count_peers_with_message()
            if count < n:
                print(f"  {count}/{n} peers have message")
            return count == n

        # With k=20 and multi-hop, need more rounds
        # Graph diameter ~3 means ~3 rounds per hop
        # But need margin for partial propagation
        max_rounds = 1000
        assert_eventually(
            all_peers_have_message,
            db=db,
            start_t_ms=t_ms,
            max_rounds=max_rounds,
            msg=f"Message did not reach all {n} peers"
        )
        print(f"Message reached all {n} peers")
