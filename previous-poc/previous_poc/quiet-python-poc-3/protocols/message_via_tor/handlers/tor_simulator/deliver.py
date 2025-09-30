def execute(input_data, db):
    """
    Converts all outgoing events to recipient peer to incoming rows (SQL-only).
    """
    delivered_count = 0
    current_time_ms = input_data.get('time_now_ms', 0)

    # SQL outgoing
    outgoing_rows = []
    if hasattr(db, 'conn'):
        try:
            import json
            cur = db.conn.cursor()
            rows = cur.execute("SELECT id, recipient, data FROM outgoing WHERE sent = 0 ORDER BY id").fetchall()
            for r in rows:
                oid = r[0]
                recipient = r[1]
                data = r[2]
                if isinstance(data, bytes):
                    data = data.decode('utf-8')
                try:
                    data_obj = json.loads(data) if isinstance(data, str) else data
                except Exception:
                    data_obj = data
                outgoing_rows.append({"id": oid, "recipient": recipient, "data": data_obj})

            # Insert into incoming table
            for row in outgoing_rows:
                env = {
                    "origin": "network",
                    "receivedAt": current_time_ms,
                    "selfGenerated": False,
                    "received_by": row["recipient"]
                }
                cur.execute(
                    "INSERT INTO incoming(recipient, data, metadata, received_at) VALUES(?, ?, ?, ?)",
                    (row["recipient"], json.dumps(row["data"]), json.dumps(env), int(current_time_ms or 0))
                )
                delivered_count += 1

            # Delete delivered outgoing (same transaction managed by runner)
            if outgoing_rows:
                ids = [r['id'] for r in outgoing_rows]
                q_marks = ','.join(['?'] * len(ids))
                cur.execute(f"DELETE FROM outgoing WHERE id IN ({q_marks})", ids)
        except Exception:
            pass

    if delivered_count == 0:
        return {"api_response": {"return": "No messages to deliver", "delivered": 0}}

    return {"api_response": {"return": f"Delivered {delivered_count} messages", "delivered": delivered_count}}
