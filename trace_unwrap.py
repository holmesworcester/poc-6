import sqlite3
from db import Database
import schema
from events import user, sync
import crypto
from events import key

conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice and Bob
alice = user.new_network(name='Alice', t_ms=1000, db=db)
bob = user.join(invite_link=alice['invite_link'], name='Bob', t_ms=2000, db=db)

alice_peer_id = alice['peer_id']
bob_peer_id = bob['peer_id']

print(f"Alice peer_id: {alice_peer_id[:20]}...")
print(f"Bob peer_id: {bob_peer_id[:20]}...")

# Check Alice's prekey in local_prekeys
alice_prekey_row = db.query_one(
    "SELECT * FROM local_prekeys WHERE owner_peer_id = ?",
    (alice_peer_id,)
)
print(f"\nAlice has prekey in local_prekeys: {alice_prekey_row is not None}")
if alice_prekey_row:
    print(f"  prekey_id: {alice_prekey_row['prekey_id'][:20]}...")

# Simulate: Bob wraps a test message to Alice's prekey
from events import prekey
alice_transit_key = prekey.get_transit_prekey_for_peer(alice_peer_id, bob_peer_id, db)
print(f"\nBob can get Alice's transit key: {alice_transit_key is not None}")

if alice_transit_key:
    test_msg = b'{"test": "message from Bob"}'
    wrapped = crypto.wrap(test_msg, alice_transit_key, db)
    print(f"Bob wrapped message, size: {len(wrapped)} bytes")
    
    # Extract hint
    hint = key.extract_id(wrapped)
    hint_b64 = crypto.b64encode(hint)
    print(f"Hint in wrapped blob: {hint_b64[:20]}...")
    print(f"Hint matches Alice's peer_id: {hint_b64 == alice_peer_id}")
    
    # Check who can decrypt
    can_decrypt = key.get_peer_ids_for_key(hint_b64, db)
    print(f"\nPeers who can decrypt: {[p[:20]+'...' for p in can_decrypt]}")
    
    # Try to unwrap as Alice
    print(f"\nAttempting unwrap as Alice...")
    unwrapped, missing = crypto.unwrap(wrapped, alice_peer_id, db)
    print(f"Unwrap succeeded: {unwrapped is not None}")
    if missing:
        print(f"Missing keys: {missing}")
    if unwrapped:
        print(f"Unwrapped message: {unwrapped}")
