def execute(input_data, db):
    """
    Provides a list of all client identities (SQL preferred, fallback to state)
    """
    identity_list = []
    
    # SQL-only
    if hasattr(db, 'conn'):
        try:
            cur = db.conn.cursor()
            rows = cur.execute("SELECT pubkey, name FROM identities ORDER BY pubkey").fetchall()
            identity_list = [
                {"identityId": r[0], "publicKey": r[0], "name": r[1]}
                for r in rows
            ]
        except Exception:
            identity_list = []
    # Dict-state deprecated; no fallback
    
    return {"api_response": {"identities": identity_list}}
