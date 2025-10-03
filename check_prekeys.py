import sqlite3
from db import Database
import schema
from events import user

conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

alice = user.new_network(name='Alice', t_ms=1000, db=db)
alice_peer_id = alice['peer_id']

print(f"Alice peer_id: {alice_peer_id}")

# Check local_prekeys
local_prekeys = db.query("SELECT * FROM local_prekeys")
print(f"\nlocal_prekeys: {len(local_prekeys)} rows")
for row in local_prekeys:
    print(f"  prekey_id={row['prekey_id'][:20]}..., owner={row['owner_peer_id'][:20]}...")

# Check pre_keys
pre_keys = db.query("SELECT * FROM pre_keys")
print(f"\npre_keys: {len(pre_keys)} rows")
for row in pre_keys:
    print(f"  peer_id={row['peer_id'][:20]}...")
    
# Try to get Alice's prekey
from events import prekey
alice_prekey = prekey.get_transit_prekey_for_peer(alice_peer_id, alice_peer_id, db)
print(f"\nAlice's transit prekey: {alice_prekey is not None}")
