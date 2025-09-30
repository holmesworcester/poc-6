def execute(input_data, db):
    """
    Purge old unknown events to prevent unbounded growth.
    SQL-first against 'unknown_events' with dict fallback.
    """
    cutoff_hours = input_data.get('cutoff_hours', 24)
    current_time = input_data.get('current_time_ms')

    if current_time is None:
        return {
            "return": "No current time provided",
            "purged": 0
        }

    cutoff_time = int(current_time - (cutoff_hours * 60 * 60 * 1000))

    # SQL-only path
    cur = db.conn.cursor()
    before = cur.execute("SELECT COUNT(1) FROM unknown_events").fetchone()[0]
    if before == 0:
        return {"return": "No unknown events", "purged": 0}
    # Purge and count remaining
    purged = cur.execute(
        "DELETE FROM unknown_events WHERE timestamp <= ?",
        (cutoff_time,),
    )
    try:
        purged_count = purged.rowcount if purged and purged.rowcount is not None else 0
    except Exception:
        purged_count = 0
    remaining = cur.execute("SELECT COUNT(1) FROM unknown_events").fetchone()[0]

    return {
        "return": f"Purged {purged_count} events",
        "purged": purged_count,
        "remaining": remaining
    }
