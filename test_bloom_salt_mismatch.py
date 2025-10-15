#!/usr/bin/env python3
"""Test if Bob's public key from peers matches Alice's view in peers_shared."""
import sqlite3
from db import Database
import schema
from events.identity import user, invite, peer, peer_shared
from events.transit import sync

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice and Bob
alice = user.new_network(name='Alice', t_ms=1000, db=db)
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

# Send bootstrap so Alice gets Bob's peer_shared
user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    transit_prekey_shared_id=bob['transit_prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

# Alice receives bootstrap
sync.receive(batch_size=20, t_ms=4100, db=db)

print("=== Public Key Comparison ===\n")

# Bob's view of his own public key
bob_own_pk = peer.get_public_key(bob['peer_id'], bob['peer_id'], db)
print(f"Bob's own public key (from peers table):")
print(f"  {bob_own_pk.hex()[:60]}...\n")

# Alice's view of Bob's public key
alice_view_bob_pk = peer_shared.get_public_key(bob['peer_shared_id'], alice['peer_id'], db)
print(f"Alice's view of Bob's public key (from peers_shared table):")
print(f"  {alice_view_bob_pk.hex()[:60]}...\n")

# Compare
print(f"Keys match: {bob_own_pk == alice_view_bob_pk}")

if bob_own_pk != alice_view_bob_pk:
    print("\n❌ MISMATCH FOUND! This would cause bloom filter to fail!")
    print(f"Bob's key:   {bob_own_pk.hex()}")
    print(f"Alice's key: {alice_view_bob_pk.hex()}")
else:
    print("\n✓ Keys match - bloom salt should be consistent")

# Test bloom filter with both keys
print("\n=== Bloom Salt Derivation ===\n")
window_id = 1

salt_bob = sync.derive_salt(bob_own_pk, window_id)
salt_alice = sync.derive_salt(alice_view_bob_pk, window_id)

print(f"Salt derived by Bob (window {window_id}):")
print(f"  {salt_bob.hex()}")
print(f"Salt derived by Alice (window {window_id}):")
print(f"  {salt_alice.hex()}")
print(f"Salts match: {salt_bob == salt_alice}")
