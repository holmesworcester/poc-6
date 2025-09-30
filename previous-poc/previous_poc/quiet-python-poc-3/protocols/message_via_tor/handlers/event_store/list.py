import json

def execute(params, db):
    """List events from the protocol's event store."""
    limit = params.get('limit', 100)
    order_desc = params.get('order_desc', True)
    
    events = []
    if hasattr(db, 'conn'):
        cur = db.conn.cursor()
        order = "DESC" if order_desc else "ASC"
        
        # Check if metadata column exists
        cur.execute("PRAGMA table_info(event_store)")
        columns = [row[1] for row in cur.fetchall()]
        has_metadata = 'metadata' in columns
        
        if has_metadata:
            cur.execute(f"""
                SELECT id, pubkey, event_data, metadata, event_type, event_id, created_at
                FROM event_store 
                ORDER BY created_at {order}
                LIMIT ?
            """, (limit,))
            
            for row in cur.fetchall():
                try:
                    event_data = json.loads(row[2]) if row[2] else {}
                    metadata = json.loads(row[3]) if row[3] else {}
                    
                    events.append({
                        'id': row[0],
                        'pubkey': row[1],
                        'event_id': row[5],
                        'event_type': row[4],
                        'payload': event_data,
                        'metadata': metadata,
                        'created_at': row[6]
                    })
                except json.JSONDecodeError:
                    continue
        else:
            # Old schema without metadata column
            cur.execute(f"""
                SELECT id, pubkey, event_data, event_type, event_id, created_at
                FROM event_store 
                ORDER BY created_at {order}
                LIMIT ?
            """, (limit,))
            
            for row in cur.fetchall():
                try:
                    event_data = json.loads(row[2]) if row[2] else {}
                    
                    events.append({
                        'id': row[0],
                        'pubkey': row[1],
                        'event_id': row[4],
                        'event_type': row[3],
                        'payload': event_data,
                        'metadata': {},
                        'created_at': row[5]
                    })
                except json.JSONDecodeError:
                    continue
    
    return {
        'api_response': {
            'events': events
        }
    }