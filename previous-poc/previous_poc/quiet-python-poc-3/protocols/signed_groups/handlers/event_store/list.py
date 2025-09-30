import json

def execute(params, db):
    """List events from the protocol's event store."""
    limit = params.get('limit', 100)
    order_desc = params.get('order_desc', True)
    
    events = []
    if hasattr(db, 'conn'):
        cur = db.conn.cursor()
        order = "DESC" if order_desc else "ASC"
        cur.execute(f"""
            SELECT id, event_id, event_type, data, metadata, created_at_ms
            FROM event_store 
            ORDER BY created_at_ms {order}
            LIMIT ?
        """, (limit,))
        
        for row in cur.fetchall():
            try:
                event_data = json.loads(row[3]) if row[3] else {}
                metadata = json.loads(row[4]) if row[4] else {}
                
                # Extract pubkey from metadata or data
                pubkey = metadata.get('received_by', event_data.get('pubkey', 'unknown'))
                
                events.append({
                    'id': row[0],
                    'event_id': row[1],
                    'event_type': row[2],
                    'pubkey': pubkey,
                    'payload': event_data,
                    'metadata': metadata,
                    'created_at': row[5]
                })
            except json.JSONDecodeError:
                continue
    
    return {
        'api_response': {
            'events': events
        }
    }