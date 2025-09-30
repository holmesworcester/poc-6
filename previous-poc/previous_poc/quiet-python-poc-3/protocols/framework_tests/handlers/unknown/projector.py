def project(db, envelope, time_now_ms):
    """
    Project unknown event types. SQL-first into 'unknown_events';
    keep dict fallback for legacy tests.
    """
    data = envelope.get('payload')
    metadata = envelope.get('metadata', {})
    ts = metadata.get('receivedAt', time_now_ms)

    # SQL-first
    try:
        if hasattr(db, 'conn') and db.conn is not None:
            cur = db.conn.cursor()
            # Check for existing event by eventId to ensure idempotency
            event_id = metadata.get('eventId')
            if event_id:
                cur.execute(
                    "SELECT 1 FROM unknown_events WHERE metadata LIKE ?",
                    (f'%"eventId": "{event_id}"%',)
                )
                if cur.fetchone():
                    return db  # Already exists, skip
            
            cur.execute(
                """
                INSERT INTO unknown_events (data, metadata, timestamp)
                VALUES (?, ?, ?)
                """,
                (__json_dump(data), __json_dump(metadata), int(ts or 0)),
            )
            
            # Also store in event_store for consistency
            if not event_id:
                # Generate event_id if missing
                import uuid
                event_id = str(uuid.uuid4())
            
            # Get pubkey from data or metadata
            pubkey = ''
            if isinstance(data, dict):
                pubkey = data.get('sender', data.get('pubkey', ''))
            if not pubkey:
                pubkey = metadata.get('sender', metadata.get('pubkey', ''))
            if not pubkey:
                pubkey = 'unknown'
            
            # Ensure pubkey exists in identities table (dummy values for unknown senders)
            try:
                row = cur.execute(
                    "SELECT 1 FROM identities WHERE pubkey = ? LIMIT 1", (pubkey,)
                ).fetchone()
                if not row:
                    cur.execute(
                        """
                        INSERT INTO identities (pubkey, privkey, name, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            pubkey,
                            '0' * 64,
                            pubkey,
                            int(ts or 0),
                            int(ts or 0),
                        ),
                    )
            except Exception:
                pass
            
            # Store in event_store
            event_type = 'unknown'
            if isinstance(data, dict) and 'type' in data:
                event_type = data['type']
            
            cur.execute(
                """
                INSERT OR IGNORE INTO event_store (pubkey, event_data, event_type, event_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pubkey, __json_dump(data), event_type, event_id, int(ts or 0)),
            )
    except Exception:
        pass

    return db


def __json_dump(obj):
    try:
        import json
        return json.dumps(obj)
    except Exception:
        return None
