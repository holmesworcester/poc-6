def execute(params, db):
    """List all groups."""
    groups = []
    
    # Read from SQL
    if hasattr(db, 'conn'):
        cur = db.conn.cursor()
        cur.execute("SELECT id, name, created_by, created_at_ms FROM groups ORDER BY created_at_ms DESC")
        
        for row in cur.fetchall():
            groups.append({
                'id': row[0],
                'name': row[1],
                'created_by': row[2],
                'created_at_ms': row[3]
            })
    
    return {
        'api_response': {
            'groups': groups
        }
    }