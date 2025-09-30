def execute(input_data, db):
    """
    Encrypt test data for real crypto tests.
    This command generates properly encrypted blobs that can be used in decrypt tests.
    """
    from core.crypto import encrypt, hash, get_crypto_mode
    import json
    
    if get_crypto_mode() != "real":
        return {"error": "This command only works in real crypto mode"}
    
    inner_data = input_data.get("inner_data")
    inner_key = input_data.get("inner_key")
    outer_key = input_data.get("outer_key")
    
    if not all([inner_data, inner_key, outer_key]):
        return {"error": "Missing required parameters: inner_data, inner_key, outer_key"}
    
    # Use the create_encrypted_blob helper
    from .process_incoming import create_encrypted_blob
    
    wire_data = create_encrypted_blob(inner_data, inner_key, outer_key)
    
    # Also return the key hashes for convenience
    outer_key_hash = hash(outer_key)
    inner_key_hash = hash(inner_key)
    
    return {
        "encrypted_blob": wire_data,
        "outer_key_hash": outer_key_hash,
        "inner_key_hash": inner_key_hash,
        "outer_key": outer_key,
        "inner_key": inner_key
    }