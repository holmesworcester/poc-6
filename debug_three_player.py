import sqlite3
from db import Database
import schema
from events import peer, key, group, channel, message, sync
import store


def run():
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create peers
    alice_peer_id, alice_peer_shared_id = peer.create(t_ms=1000, db=db)
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
    charlie_peer_id, charlie_peer_shared_id = peer.create(t_ms=3000, db=db)

    # Register prekeys
    alice_public_key = peer.get_public_key(alice_peer_id, db)
    bob_public_key = peer.get_public_key(bob_peer_id, db)
    db.execute(
        "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (bob_peer_id, bob_public_key, 4000)
    )
    db.execute(
        "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (alice_peer_id, alice_public_key, 5000)
    )
    db.commit()

    # Pre-share peer_shared between Alice and Bob
    from events import recorded
    bob_peer_shared_blob = store.get(bob_peer_shared_id, db)
    alice_sees_bob_peer_shared = store.blob(bob_peer_shared_blob, 5500, True, db)
    bob_peer_shared_fs_id = recorded.create(alice_sees_bob_peer_shared, alice_peer_id, 5500, db, True)
    recorded.project(bob_peer_shared_fs_id, db)
    alice_peer_shared_blob = store.get(alice_peer_shared_id, db)
    bob_sees_alice_peer_shared = store.blob(alice_peer_shared_blob, 5600, True, db)
    alice_peer_shared_fs_id = recorded.create(bob_sees_alice_peer_shared, bob_peer_id, 5600, db, True)
    recorded.project(alice_peer_shared_fs_id, db)
    db.commit()

    # Alice setup
    alice_key_id = key.create(alice_peer_id, t_ms=6000, db=db)
    alice_key_blob = store.get(alice_key_id, db)
    bob_sees_alice_key = store.blob(alice_key_blob, 6500, True, db)
    alice_key_fs_id = recorded.create(bob_sees_alice_key, bob_peer_id, 6500, db, True)
    recorded.project(alice_key_fs_id, db)
    alice_group_id = group.create('Alice Group', alice_peer_id, alice_peer_shared_id, alice_key_id, 7000, db)
    alice_channel_id = channel.create('general', alice_group_id, alice_peer_id, alice_peer_shared_id, alice_key_id, 8000, db)

    # Bob setup
    bob_key_id = key.create(bob_peer_id, t_ms=9000, db=db)
    bob_key_blob = store.get(bob_key_id, db)
    alice_sees_bob_key = store.blob(bob_key_blob, 9500, True, db)
    bob_key_fs_id = recorded.create(alice_sees_bob_key, alice_peer_id, 9500, db, True)
    recorded.project(bob_key_fs_id, db)
    bob_group_id = group.create('Bob Group', bob_peer_id, bob_peer_shared_id, bob_key_id, 10000, db)
    bob_channel_id = channel.create('general', bob_group_id, bob_peer_id, bob_peer_shared_id, bob_key_id, 11000, db)

    # Messages
    alice_msg = message.create_message(
        {'content': 'Hello from Alice', 'channel_id': alice_channel_id, 'group_id': alice_group_id,
         'peer_id': alice_peer_id, 'peer_shared_id': alice_peer_shared_id, 'key_id': alice_key_id}, 15000, db)
    bob_msg = message.create_message(
        {'content': 'Hello from Bob', 'channel_id': bob_channel_id, 'group_id': bob_group_id,
         'peer_id': bob_peer_id, 'peer_shared_id': bob_peer_shared_id, 'key_id': bob_key_id}, 16000, db)

    # Round 1: send requests
    sync.send_request(bob_peer_id, alice_peer_id, alice_peer_shared_id, 18000, db)
    sync.send_request(alice_peer_id, bob_peer_id, bob_peer_shared_id, 19000, db)

    # Receive requests and responses
    sync.receive(10, 22000, db)
    sync.receive(100, 23000, db)

    def print_state(label):
        print(f"\n--- {label} ---")
        print("Alice valid count:", len(db.query("SELECT 1 FROM valid_events WHERE recorded_by=?", (alice_peer_id,))))
        print("Bob valid count:", len(db.query("SELECT 1 FROM valid_events WHERE recorded_by=?", (bob_peer_id,))))
        print("Shareable events by Alice:", len(db.query("SELECT 1 FROM shareable_events WHERE peer_id=?", (alice_peer_shared_id,))))
        print("Shareable events by Bob:", len(db.query("SELECT 1 FROM shareable_events WHERE peer_id=?", (bob_peer_shared_id,))))
        print("Blocked for Alice:", db.query("SELECT * FROM blocked_events WHERE recorded_by=?", (alice_peer_id,)))
        print("Blocked deps for Alice:", db.query("SELECT * FROM blocked_event_deps WHERE recorded_by=?", (alice_peer_id,)))
        print("Blocked for Bob:", db.query("SELECT * FROM blocked_events WHERE recorded_by=?", (bob_peer_id,)))
        print("Blocked deps for Bob:", db.query("SELECT * FROM blocked_event_deps WHERE recorded_by=?", (bob_peer_id,)))
        print("Bob msg valid for Alice:", db.query_one("SELECT 1 FROM valid_events WHERE event_id=? AND recorded_by=?", (bob_msg['id'], alice_peer_id)))
        print("Alice msg valid for Bob:", db.query_one("SELECT 1 FROM valid_events WHERE event_id=? AND recorded_by=?", (alice_msg['id'], bob_peer_id)))

    print_state("after sync")
    # Inspect shareable events types
    se_rows = db.query("SELECT event_id, created_at FROM shareable_events WHERE peer_id=? ORDER BY created_at", (bob_peer_shared_id,))
    types = []
    for r in se_rows:
        blob = store.get(r['event_id'], db)
        try:
            import crypto as _c
            # Unwrap or parse
            if blob and blob[:1] in (b'{', b'['):
                data = _c.parse_json(blob)
            else:
                plain, _ = _c.unwrap(blob, db)
                data = _c.parse_json(plain) if plain else {}
            types.append(data.get('type'))
        except Exception:
            types.append('unknown')
    print('Bob shareable types:', types)
    # Show dep mapping
    print("Bob group id:", bob_group_id)
    print("Bob channel id:", bob_channel_id)
    print("Bob peer_shared id:", bob_peer_shared_id)
    print("Alice group id:", alice_group_id)
    print("Alice channel id:", alice_channel_id)
    print("Alice peer_shared id:", alice_peer_shared_id)


if __name__ == '__main__':
    run()
