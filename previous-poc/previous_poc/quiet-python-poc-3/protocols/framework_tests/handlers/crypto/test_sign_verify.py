from core.crypto import sign, verify, get_keypair

def execute(params, db):
    """Test sign and verify operations"""
    data = params["data"]
    
    # Get keypair for identity from params
    identity = params.get("identity")
    if not identity:
        raise ValueError("identity parameter is required")
    keypair = get_keypair(identity)
    
    # Sign the data
    signature = sign(data, keypair["private"])
    
    # Verify the signature
    verified = verify(data, signature, keypair["public"])
    
    return {
        "signature": signature,
        "verified": verified,
        "publicKey": keypair["public"],
        "privateKey": keypair["private"]
    }