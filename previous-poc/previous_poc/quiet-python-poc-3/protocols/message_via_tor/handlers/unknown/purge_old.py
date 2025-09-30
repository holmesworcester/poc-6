def execute(input_data, db):
    """
    Purge old unknown events from SQL to prevent unbounded growth.
    Removes rows older than the specified cutoff time.
    """
    cutoff_hours = input_data.get('cutoff_hours', 24)
    current_time = input_data.get('current_time_ms')

    if current_time is None:
        return {"return": "No current time provided", "purged": 0}

    cutoff_time = current_time - (cutoff_hours * 60 * 60 * 1000)

    purged_count = 0
    remaining = 0
    before_count = 0
    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            # Count before
            before = cur.execute("SELECT COUNT(1) AS c FROM unknown_events").fetchone()
            before_count = before[0] if before else 0
            # Delete old
            cur.execute("DELETE FROM unknown_events WHERE timestamp <= ?", (int(cutoff_time),))
            # Count after
            after = cur.execute("SELECT COUNT(1) AS c FROM unknown_events").fetchone()
            after_count = after[0] if after else 0
            purged_count = max(0, before_count - after_count)
            remaining = after_count
        except Exception:
            purged_count = 0
            remaining = 0
    else:
        # Dict-state deprecated; if no SQL connection, nothing to purge
        purged_count = 0
        remaining = 0
    
    if before_count == 0:
        return {"api_response": {"return": "No unknown events", "purged": 0}}
    return {"api_response": {"return": f"Purged {purged_count} events", "purged": purged_count, "remaining": remaining}}
