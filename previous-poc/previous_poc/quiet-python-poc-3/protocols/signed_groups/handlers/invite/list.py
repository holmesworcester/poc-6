def execute(params, db):
    """List all invites."""
    invites = []
    
    # Read from SQL
    if hasattr(db, 'conn'):
        cur = db.conn.cursor()
        cur.execute("SELECT id, invite_pubkey, network_id, group_id, created_by, created_at_ms FROM invites ORDER BY created_at_ms DESC")
        
        for row in cur.fetchall():
            invites.append({
                'id': row[0],
                'invite_pubkey': row[1],
                'network_id': row[2],
                'group_id': row[3],
                'created_by': row[4],
                'created_at_ms': row[5]
            })
    
    return {
        'api_response': {
            'invites': invites
        }
    }