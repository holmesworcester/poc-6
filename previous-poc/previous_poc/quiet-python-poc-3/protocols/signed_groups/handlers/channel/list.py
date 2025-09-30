def execute(params, db):
    """List all channels."""
    channels = []
    
    # Read from SQL
    if hasattr(db, 'conn'):
        cur = db.conn.cursor()
        
        # Get network_id filter if provided
        network_id = params.get('network_id')
        
        if network_id:
            cur.execute(
                "SELECT id, name, network_id, group_id, created_at_ms FROM channels WHERE network_id = ? ORDER BY created_at_ms DESC",
                (network_id,)
            )
        else:
            cur.execute("SELECT id, name, network_id, group_id, created_at_ms FROM channels ORDER BY created_at_ms DESC")
        
        for row in cur.fetchall():
            channels.append({
                'id': row[0],
                'name': row[1],
                'network_id': row[2],
                'group_id': row[3],
                'created_at_ms': row[4]
            })
    
    return {
        'api_response': {
            'channels': channels
        }
    }