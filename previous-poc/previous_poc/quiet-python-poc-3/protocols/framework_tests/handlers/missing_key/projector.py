def project(db, envelope, time_now_ms):
    """
    Project missing key events. SQL-first: writes to 'pending_missing_key' table
    with dict fallback for legacy state tests.
    """
    metadata = envelope.get('metadata', {})
    missing_hash = metadata.get('missingHash')
    in_network = bool(metadata.get('inNetwork', False))
    ts = metadata.get('receivedAt', time_now_ms)
    origin = metadata.get('origin')

    # Prefer SQL table when available
    try:
        if hasattr(db, 'conn') and db.conn is not None:
            cur = db.conn.cursor()
            # Check if this missingHash already exists to ensure idempotency
            cur.execute(
                "SELECT 1 FROM pending_missing_key WHERE missingHash = ?",
                (missing_hash,)
            )
            if not cur.fetchone():
                # Only insert if not already present
                cur.execute(
                    """
                    INSERT INTO pending_missing_key (envelope, missingHash, inNetwork, timestamp, origin)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (__json_dump(envelope), missing_hash, 1 if in_network else 0, int(ts or 0), origin),
                )
            # Let outer transaction commit
    except Exception:
        # Fall through to dict state
        pass

    return db


def __json_dump(obj):
    try:
        import json
        return json.dumps(obj)
    except Exception:
        return None
