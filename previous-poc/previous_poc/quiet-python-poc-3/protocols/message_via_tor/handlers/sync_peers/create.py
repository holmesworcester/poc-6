def execute(input_data, db):
    """
    Creates a sync-request event given a sender peer
    """
    sender = input_data.get("sender")
    
    if not sender:
        return {
            "api_response": {
                "return": "Error: No sender provided",
                "error": "Missing sender"
            }
        }
    
    # Create sync_peers event
    sync_event = {
        "type": "sync_peers",
        "sender": sender
    }
    
    return {
        "api_response": {
            "return": "Sync request created"
        },
        "newEvents": [sync_event]
    }