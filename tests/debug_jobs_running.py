"""Debug script to check if jobs are running."""
import sqlite3
from db import Database, create_unsafe_db
import schema
from events.identity import user, invite, peer
import jobs
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
alice = user.new_network(name='Alice', t_ms=1000, db=db)

# Alice creates an invite for Bob
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=db
)

# Bob joins Alice's network
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

db.commit()

# Check job_state before any ticks
unsafedb = create_unsafe_db(db)
job_states = unsafedb.query("SELECT job_name, last_run_at FROM job_state")
print(f"Job states before ticks: {len(job_states)}")
for state in job_states:
    print(f"  {state['job_name']}: last_run_at={state['last_run_at']}")

# Run 1 tick
print("\n=== Running tick at t_ms=4000 ===")
tick.tick(t_ms=4000, db=db)

# Check job_state after 1 tick
job_states = unsafedb.query("SELECT job_name, last_run_at FROM job_state ORDER BY job_name")
print(f"\nJob states after tick: {len(job_states)}")
for state in job_states:
    print(f"  {state['job_name']}: last_run_at={state['last_run_at']}")

# Check if SyncConnectSendJob ran
sync_connect_state = unsafedb.query_one("SELECT * FROM job_state WHERE job_name = 'sync_connect_send'")
print(f"\nSyncConnectSendJob ran: {bool(sync_connect_state)}")
if sync_connect_state:
    print(f"  last_run_at: {sync_connect_state['last_run_at']}")
