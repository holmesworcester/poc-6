def execute(params, db):
    """
    Lists blocked events from the blocked table.
    """
    blocked = []
    cur = db.conn.cursor()
    rows = cur.execute(
        "SELECT event_id, blocked_by_id, reason FROM blocked ORDER BY created_at_ms"
    ).fetchall()
    for r in rows:
        blocked.append({
            'event_id': r[0], 
            'blocked_by': r[1], 
            'reason': r[2]
        })
    return {'api_response': {'blocked': blocked}}
