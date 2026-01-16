"""Profile projection performance to understand bottlenecks."""
import sqlite3
import cProfile
import pstats
import io
import time
from pathlib import Path

# Setup path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.db import Database
from core import schema
from events.identity import user, invite, peer
from events.content import message
from tests.utils.tick_helper import run_ticks
from core import tick


def setup_scenario():
    """Create a test scenario with multiple peers and messages."""
    db_path = Path("/tmp/profile_test.db")
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    db = Database(conn)
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = MEMORY")
    schema.create_all(db)

    # Create Alice's network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    db.commit()
    return db, alice, bob


def run_sync_profile(db, num_rounds=50):
    """Run sync ticks and profile the projection."""
    run_ticks(db=db, start_t_ms=3000, num_rounds=num_rounds)


def run_message_profile(db, alice, bob, num_messages=20):
    """Create and sync messages, profile the projection."""
    alice_channel_id = db.query_one(
        "SELECT channel_id FROM channels WHERE recorded_by = ? LIMIT 1",
        (alice['peer_id'],)
    )['channel_id']

    t_ms = 10000
    for i in range(num_messages):
        message.create(
            peer_id=alice['peer_id'],
            channel_id=alice_channel_id,
            content=f"Message {i} from Alice",
            t_ms=t_ms + i * 100,
            db=db
        )
        message.create(
            peer_id=bob['peer_id'],
            channel_id=alice_channel_id,
            content=f"Message {i} from Bob",
            t_ms=t_ms + i * 100 + 50,
            db=db
        )

    db.commit()

    # Sync to deliver messages
    run_ticks(db=db, start_t_ms=t_ms + num_messages * 100 + 1000, num_rounds=30)


def profile_scenario():
    """Full profiling scenario."""
    print("Setting up scenario...")
    db, alice, bob = setup_scenario()

    print("\n=== Profiling sync (50 rounds) ===")
    profiler = cProfile.Profile()
    profiler.enable()

    start = time.time()
    run_sync_profile(db, num_rounds=50)
    sync_time = time.time() - start

    profiler.disable()

    # Print stats focused on our code
    s = io.StringIO()
    ps = pstats.Stats(profiler, stream=s).sort_stats('cumulative')
    ps.print_stats(50)

    print(s.getvalue())
    print(f"Sync time: {sync_time:.3f}s")

    # Filter for our modules
    print("\n=== Key functions (projection/resolver) ===")
    s2 = io.StringIO()
    ps2 = pstats.Stats(profiler, stream=s2).sort_stats('cumulative')
    ps2.print_stats('resolver|recorded|projection|store|check_deps|_is_event_valid|_fetch_dep')
    print(s2.getvalue())

    print("\n=== Profiling messages (20 messages each) ===")
    profiler2 = cProfile.Profile()
    profiler2.enable()

    start = time.time()
    run_message_profile(db, alice, bob, num_messages=20)
    msg_time = time.time() - start

    profiler2.disable()

    s3 = io.StringIO()
    ps3 = pstats.Stats(profiler2, stream=s3).sort_stats('cumulative')
    ps3.print_stats('resolver|recorded|projection|store|check_deps|_is_event_valid|_fetch_dep|query')
    print(s3.getvalue())
    print(f"Message time: {msg_time:.3f}s")

    # Count DB queries
    print("\n=== Database query analysis ===")
    # Check how many events were processed
    total_events = db.query_one("SELECT COUNT(*) as c FROM store")['c']
    valid_events = db.query_one("SELECT COUNT(*) as c FROM valid_events")['c']
    blocked_events = db.query_one("SELECT COUNT(*) as c FROM blocked_events_ephemeral")['c']

    print(f"Total events stored: {total_events}")
    print(f"Valid events: {valid_events}")
    print(f"Blocked events: {blocked_events}")

    db._conn.close()


def compare_with_without_cache():
    """Compare performance with and without RETE cache."""
    import time

    # Test with cache (default)
    print("\n=== Testing WITH cache (RETE optimization) ===")
    db1, alice1, bob1 = setup_scenario()
    start = time.time()
    run_sync_profile(db1, num_rounds=30)
    run_message_profile(db1, alice1, bob1, num_messages=15)
    with_cache_time = time.time() - start
    db1._conn.close()

    print(f"Time WITH cache: {with_cache_time:.3f}s")

    # Note: To test without cache, we'd need to modify the code to disable it
    # For now, this just demonstrates the optimized path works correctly


if __name__ == '__main__':
    profile_scenario()
