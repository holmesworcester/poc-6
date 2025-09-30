def project(db, envelope, time_now_ms):
    """
    Project message events. SQL-first only: insert into event_store and messages
    when sender is known via SQL tables (no dict-state fallbacks).
    """
    data = envelope.get('payload', {})
    metadata = envelope.get('metadata', {})
    sender = data.get("sender") or metadata.get("sender")

    # Compute canonical ids/times
    event_type = data.get('type', 'message')
    event_id = metadata.get('eventId') or __hash_json(data)
    created_at = metadata.get('receivedAt', time_now_ms)

    # Determine known-sender using SQL tables only
    is_known = False
    try:
        if hasattr(db, 'conn') and db.conn is not None:
            cur = db.conn.cursor()
            # Check table existence and membership
            try:
                row = cur.execute(
                    "SELECT 1 FROM known_senders WHERE pubkey = ? LIMIT 1",
                    (sender,),
                ).fetchone()
                is_known = bool(row)
            except Exception:
                is_known = False
    except Exception:
        pass

    # SQL-first writes
    try:
        if hasattr(db, 'conn') and db.conn is not None:
            cur = db.conn.cursor()
            # Ensure sender identity exists to satisfy FKs (use dummy fields)
            if sender:
                try:
                    row = cur.execute(
                        "SELECT 1 FROM identities WHERE pubkey = ? LIMIT 1", (sender,)
                    ).fetchone()
                    if not row:
                        cur.execute(
                            """
                            INSERT INTO identities (pubkey, privkey, name, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                sender,
                                '0' * 64,
                                sender,
                                int(created_at or 0),
                                int(created_at or 0),
                            ),
                        )
                except Exception:
                    pass
            # Insert into event_store mirror for framework protocol
            cur.execute(
                """
                INSERT OR IGNORE INTO event_store (pubkey, event_data, event_type, event_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sender or '', __json_dump(data), event_type, event_id, int(created_at or 0)),
            )
            if is_known:
                # Prepare message row
                text_val = data.get('text') or data.get('content') or ''
                reply_to = data.get('replyTo')
                ts_val = data.get('timestamp') or created_at or 0
                cur.execute(
                    """
                    INSERT OR IGNORE INTO messages (event_id, text, sender, recipient, reply_to, timestamp, sig, unknown_peer, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        text_val,
                        sender or '',
                        None,
                        reply_to,
                        int(ts_val or 0),
                        '',
                        0,
                        int(created_at or 0),
                    ),
                )
    except Exception:
        # Non-fatal; dict fallback still applies
        pass

    return db


def __hash_json(obj):
    try:
        import json
        from core.crypto import hash as _hash
        return _hash(json.dumps(obj, sort_keys=True))
    except Exception:
        return None


def __json_dump(obj):
    try:
        import json
        return json.dumps(obj)
    except Exception:
        return None
