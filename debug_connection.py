"""Debug script to trace connection flow."""
import sqlite3
import logging
from db import Database
import schema
from events.identity import user, invite, peer
from events.network import connection
import queues
import crypto
import tick

# Suppress most logging
logging.getLogger().setLevel(logging.ERROR)

def test_connection_flow():
    conn = sqlite3.connect(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("=== Creating Alice's network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    alice_peer_shared = db.query_one("SELECT peer_shared_id FROM peer_self WHERE peer_id = ?", (alice['peer_id'],))['peer_shared_id']
    print(f"Alice peer_id: {alice['peer_id'][:20]}...")
    print(f"Alice peer_shared_id: {alice_peer_shared[:20]}...")

    # Create invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'], t_ms=1500, db=db
    )

    print("\n=== Bob joins ===")
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared = db.query_one("SELECT peer_shared_id FROM peer_self WHERE peer_id = ?", (bob['peer_id'],))['peer_shared_id']
    print(f"Bob peer_id: {bob['peer_id'][:20]}...")
    print(f"Bob peer_shared_id: {bob_peer_shared[:20]}...")

    db.commit()

    print("\n=== BEFORE ticks: peers_shared count ===")
    for peer_id in [alice['peer_id'], bob['peer_id']]:
        name = "Alice" if peer_id == alice['peer_id'] else "Bob"
        rows = db.query_all("SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?", (peer_id,))
        print(f"{name} knows {len(rows)} peers_shared")
        for r in rows[:5]:
            print(f"  - {r['peer_shared_id'][:20]}...")

    print("\n=== Local peers before ticks ===")
    local_peers = db.query_all("SELECT peer_id FROM local_peers")
    print(f"Number of local_peers: {len(local_peers)}")
    for lp in local_peers:
        print(f"  - {lp['peer_id'][:20]}...")

    print("\n=== Running 3 ticks ===")
    for i in range(3):
        t_ms = 3000 + (i * 100)
        tick.tick(t_ms=t_ms, db=db)
        # Check connections after each tick
        conn_count = db.query_one("SELECT COUNT(*) as cnt FROM connections")['cnt']
        print(f"  Tick {i+1} (t={t_ms}): {conn_count} total connections")

    print("\n=== AFTER ticks: peers_shared count ===")
    for peer_id in [alice['peer_id'], bob['peer_id']]:
        name = "Alice" if peer_id == alice['peer_id'] else "Bob"
        rows = db.query_all("SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?", (peer_id,))
        print(f"{name} knows {len(rows)} peers_shared")
        for r in rows[:5]:
            print(f"  - {r['peer_shared_id'][:20]}...")

    print("\n=== Connection summary ===")
    alice_conns = db.query_all("SELECT peer_shared_id, their_key IS NOT NULL as active FROM connections WHERE recorded_by = ?", (alice['peer_id'],))
    bob_conns = db.query_all("SELECT peer_shared_id, their_key IS NOT NULL as active FROM connections WHERE recorded_by = ?", (bob['peer_id'],))

    print(f"Alice has {len(alice_conns)} connections ({sum(1 for c in alice_conns if c['active'])} active)")
    print(f"Bob has {len(bob_conns)} connections ({sum(1 for c in bob_conns if c['active'])} active)")

    # Check unique connection_ids
    alice_conn_ids = db.query_all("SELECT DISTINCT connection_id FROM connections WHERE recorded_by = ?", (alice['peer_id'],))
    bob_conn_ids = db.query_all("SELECT DISTINCT connection_id FROM connections WHERE recorded_by = ?", (bob['peer_id'],))
    print(f"Alice has {len(alice_conn_ids)} unique connection_ids")
    print(f"Bob has {len(bob_conn_ids)} unique connection_ids")

    # Check if Alice and Bob have an active connection to each other
    alice_to_bob = db.query_one("""
        SELECT their_key IS NOT NULL as active FROM connections
        WHERE recorded_by = ? AND peer_shared_id = ?
    """, (alice['peer_id'], bob_peer_shared))
    bob_to_alice = db.query_one("""
        SELECT their_key IS NOT NULL as active FROM connections
        WHERE recorded_by = ? AND peer_shared_id = ?
    """, (bob['peer_id'], alice_peer_shared))

    print(f"\nAlice → Bob: {'ACTIVE' if alice_to_bob and alice_to_bob['active'] else 'PENDING or NONE'}")
    print(f"Bob → Alice: {'ACTIVE' if bob_to_alice and bob_to_alice['active'] else 'PENDING or NONE'}")

    # Count unique peer_shared_ids in connections
    print("\n=== Unique peer_shared_ids in connections ===")
    alice_ps_ids = set(c['peer_shared_id'] for c in alice_conns if c['peer_shared_id'])
    bob_ps_ids = set(c['peer_shared_id'] for c in bob_conns if c['peer_shared_id'])

    print(f"Alice connects to {len(alice_ps_ids)} unique peer_shared_ids")
    for ps in alice_ps_ids:
        is_self = "SELF" if ps == alice_peer_shared else ""
        is_bob = "BOB" if ps == bob_peer_shared else ""
        label = is_self or is_bob or "UNKNOWN"
        count = sum(1 for c in alice_conns if c['peer_shared_id'] == ps)
        print(f"  {ps[:20]}... ({label}) x{count}")

    print(f"Bob connects to {len(bob_ps_ids)} unique peer_shared_ids")
    for ps in bob_ps_ids:
        is_self = "SELF" if ps == bob_peer_shared else ""
        is_alice = "ALICE" if ps == alice_peer_shared else ""
        label = is_self or is_alice or "UNKNOWN"
        count = sum(1 for c in bob_conns if c['peer_shared_id'] == ps)
        print(f"  {ps[:20]}... ({label}) x{count}")

    # Check for 'PENDING' connections
    print("\n=== Checking for PENDING connections ===")
    pending_conns = db.query_all("SELECT COUNT(*) as cnt FROM connections WHERE peer_shared_id = 'PENDING'")
    print(f"Connections to 'PENDING': {pending_conns[0]['cnt']}")

    # Check invite_accepteds for inviter_peer_shared_id
    print("\n=== Checking invite_accepteds ===")
    alice_invite = db.query_all("SELECT inviter_peer_shared_id FROM invite_accepteds WHERE recorded_by = ?", (alice['peer_id'],))
    bob_invite = db.query_all("SELECT inviter_peer_shared_id FROM invite_accepteds WHERE recorded_by = ?", (bob['peer_id'],))
    for i in alice_invite:
        inv_ps = i['inviter_peer_shared_id'][:30] if i['inviter_peer_shared_id'] else 'NULL'
        is_pending = " (PENDING!)" if inv_ps == 'PENDING' else ""
        is_self = " (ALICE)" if i['inviter_peer_shared_id'] == alice_peer_shared else ""
        is_bob = " (BOB)" if i['inviter_peer_shared_id'] == bob_peer_shared else ""
        print(f"Alice's invite_accepted: inviter={inv_ps}{is_pending}{is_self}{is_bob}")
    for i in bob_invite:
        inv_ps = i['inviter_peer_shared_id'][:30] if i['inviter_peer_shared_id'] else 'NULL'
        is_pending = " (PENDING!)" if inv_ps == 'PENDING' else ""
        is_self = " (BOB)" if i['inviter_peer_shared_id'] == bob_peer_shared else ""
        is_alice = " (ALICE)" if i['inviter_peer_shared_id'] == alice_peer_shared else ""
        print(f"Bob's invite_accepted: inviter={inv_ps}{is_pending}{is_self}{is_alice}")

if __name__ == "__main__":
    test_connection_flow()
