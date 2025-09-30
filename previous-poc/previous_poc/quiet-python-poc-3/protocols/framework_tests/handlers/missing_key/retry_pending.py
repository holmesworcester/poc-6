def execute(input_data, db):
    """
    Retry pending missing key envelopes when key_map has changed.
    SQL-first against 'pending_missing_key' and 'key_map';
    falls back to dict state.
    """
    # SQL-only path
    cur = db.conn.cursor()
    known_rows = cur.execute("SELECT key_hash FROM key_map").fetchall()
    known_keys = set(r[0] for r in known_rows)
    pend_rows = cur.execute("SELECT id, missingHash FROM pending_missing_key").fetchall()
    if not pend_rows:
        return {"return": "No pending entries", "processed": 0}
    ids_to_delete = [r[0] for r in pend_rows if r[1] in known_keys]
    processed_count = 0
    if ids_to_delete:
        cur.execute(
            f"DELETE FROM pending_missing_key WHERE id IN ({','.join(['?']*len(ids_to_delete))})",
            ids_to_delete,
        )
        processed_count = len(ids_to_delete)
    remaining = cur.execute("SELECT COUNT(1) FROM pending_missing_key").fetchone()[0]

    return {
        "return": f"Processed {processed_count} entries",
        "processed": processed_count,
        "remaining": remaining
    }
