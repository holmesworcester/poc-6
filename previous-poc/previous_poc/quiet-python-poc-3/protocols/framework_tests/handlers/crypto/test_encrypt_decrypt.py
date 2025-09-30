from core.crypto import encrypt, decrypt, hash

def execute(params, db):
    """Test encrypt and decrypt operations"""
    data = params["data"]
    
    # Generate or use provided key
    if "key" in params:
        key = params["key"]
    else:
        # Generate a key from identity if provided in params
        identity = params.get("identity", "default")
        key = hash(f"{identity}_test_key")[:64]  # 32 bytes hex
    
    # Encrypt the data
    encrypted_result = encrypt(data, key)
    
    # Decrypt the data
    decrypted = decrypt(
        encrypted_result["ciphertext"],
        encrypted_result["nonce"],
        key
    )
    
    # Check if decryption matches original
    matches = decrypted.decode() == data if decrypted else False
    
    return {
        "encrypted": encrypted_result,
        "decrypted": decrypted.decode() if decrypted else None,
        "matches": matches
    }