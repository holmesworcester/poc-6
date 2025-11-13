import sqlite3
import logging
from db import Database
import schema
from events.identity import user, link_invite, link
from events.content import message as msg
import tick


def run():
    logging.getLogger().setLevel(logging.CRITICAL)
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()
    _, link_url, _ = link_invite.create(peer_id=alice_phone['peer_id'], t_ms=2000, db=db)
    db.commit()
    alice_laptop = link.join(link_url=link_url, t_ms=3000, db=db)
    db.commit()

    # Establish connections and initial GKS
    for i in range(60):
        tick.tick(t_ms=4000 + i * 100, db=db)

    # Create messages both sides
    msg.create(peer_id=alice_phone['peer_id'], channel_id=alice_phone['channel_id'], content='P->L', t_ms=7000, db=db)
    db.commit()
    msg.create(peer_id=alice_laptop['peer_id'], channel_id=alice_phone['channel_id'], content='L->P', t_ms=7100, db=db)
    db.commit()

    for i in range(20):
        tick.tick(t_ms=8000 + i * 200, db=db)

    phone_msgs = msg.list_messages(alice_phone['channel_id'], alice_phone['peer_id'], db)
    laptop_msgs = msg.list_messages(alice_phone['channel_id'], alice_laptop['peer_id'], db)
    print('Phone messages:', [m['content'] for m in phone_msgs])
    print('Laptop messages:', [m['content'] for m in laptop_msgs])

    # Inspect shareable events ownership
    cur = db._conn.cursor()
    phone_shareable = cur.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id=?",
        (alice_phone['peer_id'],)
    ).fetchone()[0]
    laptop_shareable = cur.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id=?",
        (alice_laptop['peer_id'],)
    ).fetchone()[0]
    print('shareable by phone:', phone_shareable, 'shareable by laptop:', laptop_shareable)

    # Check if phone knows laptop peer_shared
    knows = cur.execute(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_laptop['peer_shared_id'], alice_phone['peer_id'])
    ).fetchone()
    print('Phone knows laptop peer_shared:', bool(knows))

    # Sync connections table
    conns = list(db._conn.execute("SELECT peer_shared_id, last_seen_ms FROM sync_connections"))
    print('sync_connections:', [(r['peer_shared_id'][:10], r['last_seen_ms']) for r in conns])

    # For each connection, inspect sync_state_ephemeral for both directions
    rows = list(db._conn.execute("SELECT from_peer_id, to_peer_id, last_window, w_param FROM sync_state_ephemeral"))
    print('sync_state_ephemeral:', rows)


if __name__ == '__main__':
    run()
