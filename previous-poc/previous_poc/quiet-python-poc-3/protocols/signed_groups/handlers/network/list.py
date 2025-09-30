def execute(params, db):
    """List all networks."""
    networks = []
    
    # Read from SQL
    if hasattr(db, 'conn'):
        cur = db.conn.cursor()
        cur.execute("SELECT id, name, created_at_ms FROM networks ORDER BY created_at_ms DESC")
        for row in cur.fetchall():
            networks.append({
                'id': row[0],
                'name': row[1],
                'created_at_ms': row[2]
            })
    
    return {
        'api_response': {
            'networks': networks
        }
    }