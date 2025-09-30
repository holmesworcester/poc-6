"""List users"""

def execute(params, db):
    """
    List users, optionally filtered by network
    
    Args:
        params: dict with:
            - network_id: Optional network ID to filter by
        db: Database instance
        time_now_ms: Current timestamp in milliseconds
        
    Returns:
        dict with users list
    """
    network_id = params.get('network_id')
    
    # Query users from the database
    cursor = db.conn.cursor()
    
    if network_id:
        cursor.execute("""
            SELECT 
                id,
                pubkey,
                network_id,
                name,
                group_id
            FROM users 
            WHERE network_id = ?
            ORDER BY name
        """, (network_id,))
    else:
        cursor.execute("""
            SELECT 
                id,
                pubkey,
                network_id,
                name,
                group_id
            FROM users 
            ORDER BY name
        """)
    
    users = []
    for row in cursor.fetchall():
        user = {
            'id': row[0],
            'pubkey': row[1],
            'network_id': row[2],
            'name': row[3]
        }
        # Only include group_id if it's not None
        if row[4] is not None:
            user['group_id'] = row[4]
        users.append(user)
    
    return {
        'api_response': {
            'users': users
        }
    }