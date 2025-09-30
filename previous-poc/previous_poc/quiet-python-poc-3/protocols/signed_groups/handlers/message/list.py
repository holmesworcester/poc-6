"""List messages in a channel"""

def execute(params, db, time_now_ms=None):
    """
    List messages in a channel
    
    Args:
        params: dict with:
            - channel_id: ID of channel to get messages from
            - limit: Optional max number of messages (default 100)
        db: Database instance
        time_now_ms: Current timestamp in milliseconds
        
    Returns:
        dict with messages list
    """
    channel_id = params.get('channel_id')
    if not channel_id:
        raise ValueError("channel_id is required")
    
    limit = params.get('limit', 100)
    
    # Query messages from the database
    cursor = db.conn.cursor()
    cursor.execute("""
        SELECT 
            id,
            channel_id,
            user_id,
            peer_id,
            content,
            created_at_ms,
            author_id
        FROM messages 
        WHERE channel_id = ?
        ORDER BY created_at_ms ASC
    """, (channel_id,))
    
    messages = []
    for row in cursor.fetchall():
        messages.append({
            'id': row[0],
            'channel_id': row[1],
            'user_id': row[2],
            'peer_id': row[3],
            'content': row[4],
            'created_at_ms': row[5],
            'author_id': row[6]
        })
    
    # If limit is specified, return only the most recent messages
    if limit and len(messages) > limit:
        messages = messages[-limit:]
    
    return {
        'api_response': {
            'messages': messages
        }
    }