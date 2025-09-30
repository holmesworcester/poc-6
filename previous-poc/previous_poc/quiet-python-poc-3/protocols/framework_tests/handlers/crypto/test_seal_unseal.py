from core.crypto import seal, unseal, get_keypair

def execute(params, db):
    """Test seal and unseal operations"""
    data = params["data"]
    
    # Get keypair for identity from params
    identity = params.get("identity")
    if not identity:
        raise ValueError("identity parameter is required")
    keypair = get_keypair(identity)
    
    # Seal the data for this identity's public key
    sealed = seal(data, keypair["public"])
    
    # Unseal the data with private key
    unsealed = unseal(sealed, keypair["private"], keypair["public"])
    
    # Check if unsealing matches original
    matches = unsealed.decode() == data if unsealed else False
    
    return {
        "sealed": sealed,
        "unsealed": unsealed.decode() if unsealed else None,
        "matches": matches,
        "publicKey": keypair["public"]
    }