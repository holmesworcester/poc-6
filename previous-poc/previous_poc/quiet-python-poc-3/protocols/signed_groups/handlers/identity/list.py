import json

def execute(params, db):
    """List all identities."""
    identities = []
    
    # Read from SQL
    if hasattr(db, 'conn'):
        cur = db.conn.cursor()
        cur.execute("SELECT pubkey, name FROM identities ORDER BY created_at_ms DESC")
        for row in cur.fetchall():
            identities.append({
                'pubkey': row[0],
                'name': row[1]
            })
    
    # Return list of identities
    return {
        'api_response': {
            'identities': identities
        }
    }