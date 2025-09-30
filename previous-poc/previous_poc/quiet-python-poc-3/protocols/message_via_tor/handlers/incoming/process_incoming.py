from core.handle import handle
import uuid
import time
import json

MANAGE_TRANSACTIONS = True


def execute(input_data, db):
    """
    Process incoming message queue (SQL-backed) per-item with its own transaction.
    Each iteration: BEGIN IMMEDIATE; select oldest row; project; delete; COMMIT.
    """
    current_time = input_data.get("time_now_ms", int(time.time() * 1000))
    processed = 0

    if not hasattr(db, 'begin_transaction'):
        return {"api_response": {"processed": 0}}

    while True:
        try:
            db.begin_transaction()
        except Exception:
            break

        try:
            cur = db.conn.cursor()
            r = cur.execute("SELECT id, recipient, data, metadata FROM incoming ORDER BY id LIMIT 1").fetchone()
            if not r:
                # Nothing to process
                db.rollback()
                break

            rid = r[0]
            recipient = r[1]
            data = r[2]
            metadata = r[3]
            try:
                data = json.loads(data) if isinstance(data, str) else data
            except Exception:
                pass
            try:
                metadata = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
            except Exception:
                metadata = {}

            envelope = {
                "envelope": True,
                "recipient": recipient,
                "payload": data,
                "metadata": metadata or {}
            }
            if 'eventId' not in envelope['metadata']:
                envelope['metadata']['eventId'] = str(uuid.uuid4())
            if 'timestamp' not in envelope['metadata']:
                envelope['metadata']['timestamp'] = current_time
            if 'received_by' not in envelope['metadata']:
                envelope['metadata']['received_by'] = recipient

            # Project within the same transaction
            handle(db, envelope, input_data.get("time_now_ms"), auto_transaction=False)

            # Remove from queue
            cur.execute("DELETE FROM incoming WHERE id = ?", (rid,))
            db.commit()
            processed += 1
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            # Leave the row for future attempts
            break

    return {"api_response": {"processed": processed}}
