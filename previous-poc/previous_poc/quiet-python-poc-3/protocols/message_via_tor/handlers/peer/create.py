def execute(input_data, db):
    """
    Creates and returns a new peer event
    """
    # API expects publicKey
    pubkey = input_data.get("publicKey")
    name = input_data.get("name", pubkey[:8] if pubkey else "Unknown")
    
    if not pubkey:
        return {
            "api_response": {
                "return": "Error: No pubkey provided",
                "error": "Missing pubkey"
            }
        }
    
    # Create peer event
    peer_event = {
        "type": "peer",
        "pubkey": pubkey,
        "name": name
    }
    
    return {
        "api_response": {
            "return": "Peer created",
            "peer": {
                "pubkey": pubkey,
                "name": name
            }
        },
        "newEvents": [peer_event]
    }