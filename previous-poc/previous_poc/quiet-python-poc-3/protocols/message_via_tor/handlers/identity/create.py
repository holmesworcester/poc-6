from core.crypto import generate_keypair
import json


def execute(input_data, db):
    """
    Creates an identity containing pubkey, privkey, and calls peer.create
    """
    # Generate a new keypair
    keypair = generate_keypair()
    privkey = keypair["private"]
    pubkey = keypair["public"]
    
    # Create identity event
    identity_event = {
        "type": "identity",
        "pubkey": pubkey,
        "privkey": privkey,
        "name": input_data.get("name", pubkey[:8])  # Default name to first 8 chars of pubkey
    }
    
    # Also create a peer event for this identity
    peer_event = {
        "type": "peer",
        "pubkey": pubkey,
        "name": input_data.get("name", pubkey[:8])
    }
    
    return {
        "api_response": {
            "identityId": pubkey,
            "publicKey": pubkey
        },
        "newEvents": [identity_event, peer_event]
    }