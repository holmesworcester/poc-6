import os
import json
import hashlib

try:
    import nacl.signing
    import nacl.encoding
    import nacl.secret
    import nacl.utils
    import nacl.public
    import nacl.pwhash
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False

# Global state for crypto mode
def get_crypto_mode():
    # Use the mode specified by the test/environment
    mode = os.environ.get("CRYPTO_MODE", "real")
    
    # If real mode requested but nacl not available, raise error
    if mode == "real" and not NACL_AVAILABLE:
        raise ImportError("PyNaCl is required for real crypto mode. Install with: pip install pynacl")
    
    return mode

# Core crypto primitives for framework use

def generate_keypair():
    """
    Generate a new keypair.
    In dummy mode, returns predictable keys.
    """
    if get_crypto_mode() == "dummy":
        # Dummy mode - generate a random identifier with consistent pub/priv naming
        import random
        import string
        rand_id = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        return {
            "public": f"{rand_id}_pub",
            "private": f"{rand_id}_priv"
        }
    else:
        # Real mode - generate new keys
        signing_key = nacl.signing.SigningKey.generate()
        return {
            "public": signing_key.verify_key.encode(nacl.encoding.HexEncoder).decode(),
            "private": signing_key.encode(nacl.encoding.HexEncoder).decode()
        }

def sign(data, private_key):
    """
    Sign data with private key.
    Args:
        data: bytes or string to sign
        private_key: hex-encoded private key or raw bytes
    Returns:
        hex-encoded signature
    """
    if isinstance(data, str):
        data = data.encode()
    
    if get_crypto_mode() == "dummy":
        # Dummy signature format: dummy_sig_<base64_data>_by_<public_key>
        # This ensures we're signing the exact data that would be signed in real mode
        # In dummy mode, we derive the public key from the private key name
        if isinstance(private_key, str):
            if private_key.endswith("_priv"):
                public_key = private_key.replace("_priv", "_pub")
            elif private_key.startswith("dummy_privkey_"):
                # For generated dummy keys
                public_key = private_key.replace("dummy_privkey_", "dummy_pubkey_")
            else:
                # Fallback - use the private key itself as identifier
                public_key = private_key
        else:
            public_key = str(private_key)
        
        # Encode the data in base64 for the signature
        import base64
        data_b64 = base64.b64encode(data).decode('ascii')
        
        return f"dummy_sig_{data_b64}_by_{public_key}"
    
    # Real signature
    if isinstance(private_key, str):
        signing_key = nacl.signing.SigningKey(private_key, encoder=nacl.encoding.HexEncoder)
    else:
        signing_key = nacl.signing.SigningKey(private_key)
    
    signed = signing_key.sign(data)
    return nacl.encoding.HexEncoder.encode(signed.signature).decode()


def verify(data, signature, public_key):
    """
    Verify signature on data.
    Args:
        data: bytes or string that was signed
        signature: hex-encoded signature
        public_key: hex-encoded public key
    Returns:
        bool - True if valid, False otherwise
    """
    if isinstance(data, str):
        data = data.encode()
    
    if get_crypto_mode() == "dummy":
        # Dummy verification - check signature format with base64 encoded data
        if signature.startswith("dummy_sig_") and "_by_" in signature:
            # Format: dummy_sig_<base64_data>_by_<public_key>
            try:
                # Split to get data and signer parts
                parts = signature.split("_by_")
                if len(parts) != 2:
                    return False
                
                data_part = parts[0].replace("dummy_sig_", "")
                signer_part = parts[1]
                
                # Decode the data from base64 and compare
                import base64
                expected_data = base64.b64decode(data_part)
                
                # Verify the data matches exactly
                if expected_data != data:
                    return False
                
                # Verify the signer matches
                return signer_part == public_key
            except Exception:
                return False
        elif signature.startswith("dummy_sig_signed_by_"):
            # Old format for backward compatibility: dummy_sig_signed_by_<public_key>
            expected_signer = signature.replace("dummy_sig_signed_by_", "")
            return expected_signer == public_key
        else:
            return False
    
    # Real verification
    try:
        verify_key = nacl.signing.VerifyKey(public_key, encoder=nacl.encoding.HexEncoder)
        verify_key.verify(data, nacl.encoding.HexEncoder.decode(signature))
        return True
    except:
        return False


def encrypt(data, key):
    """
    Encrypt data with symmetric key.
    Args:
        data: bytes or string to encrypt
        key: 32-byte key or hex-encoded key
    Returns:
        dict with ciphertext (hex), nonce (hex), and algorithm
    """
    if isinstance(data, str):
        data = data.encode()
    
    if get_crypto_mode() == "dummy":
        # Dummy encryption
        return {
            "ciphertext": f"dummy_encrypted_{data.decode() if isinstance(data, bytes) else data}",
            "nonce": "dummy_nonce",
            "algorithm": "dummy"
        }
    
    # Real encryption
    if isinstance(key, str):
        key = nacl.encoding.HexEncoder.decode(key)
    elif len(key) != 32:
        # Derive key if not 32 bytes
        key = hashlib.blake2b(key, digest_size=32).digest()
    
    box = nacl.secret.SecretBox(key)
    encrypted = box.encrypt(data)
    
    return {
        "ciphertext": nacl.encoding.HexEncoder.encode(encrypted.ciphertext).decode(),
        "nonce": nacl.encoding.HexEncoder.encode(encrypted.nonce).decode(),
        "algorithm": "nacl_secretbox"
    }


def decrypt(ciphertext, nonce, key):
    """
    Decrypt data with symmetric key.
    Args:
        ciphertext: hex-encoded ciphertext
        nonce: hex-encoded nonce
        key: 32-byte key or hex-encoded key
    Returns:
        decrypted bytes or None if decryption fails
    """
    if get_crypto_mode() == "dummy":
        # Dummy decryption
        if isinstance(ciphertext, str) and ciphertext.startswith("dummy_encrypted_"):
            return ciphertext[len("dummy_encrypted_"):].encode()
        return None
    
    # Real decryption
    try:
        if isinstance(key, str):
            key = nacl.encoding.HexEncoder.decode(key)
        elif len(key) != 32:
            key = hashlib.blake2b(key, digest_size=32).digest()
        
        box = nacl.secret.SecretBox(key)
        
        ciphertext_bytes = nacl.encoding.HexEncoder.decode(ciphertext)
        nonce_bytes = nacl.encoding.HexEncoder.decode(nonce)
        
        # Reconstruct the encrypted message
        encrypted = nacl.utils.EncryptedMessage(nonce_bytes + ciphertext_bytes)
        
        return box.decrypt(encrypted)
    except Exception as e:
        import os
        if os.environ.get("DEBUG_CRYPTO"):
            print(f"[crypto.decrypt] Error: {e}")
        return None


def hash(data, algorithm="blake2b"):
    """
    Hash data using blake2b algorithm.
    Args:
        data: bytes or string to hash
        algorithm: hash algorithm (only blake2b supported)
    Returns:
        hex-encoded hash
    """
    if isinstance(data, str):
        data = data.encode()
    
    if algorithm != "blake2b":
        raise ValueError(f"Unsupported hash algorithm: {algorithm}. Only blake2b is supported.")
    
    if get_crypto_mode() == "dummy" or not NACL_AVAILABLE:
        # Use hashlib blake2b for dummy mode
        return hashlib.blake2b(data).hexdigest()
    else:
        # Use nacl blake2b for real mode
        import nacl.hash
        return nacl.encoding.HexEncoder.encode(
            nacl.hash.blake2b(data, encoder=nacl.encoding.RawEncoder)
        ).decode()


def seal(data, recipient_public_key):
    """
    Seal data for a specific recipient (anonymous encryption).
    Args:
        data: bytes or string to seal
        recipient_public_key: hex-encoded public key
    Returns:
        hex-encoded sealed box
    """
    if isinstance(data, str):
        data = data.encode()
    
    if get_crypto_mode() == "dummy":
        # Dummy seal
        return f"dummy_sealed_{data.decode() if isinstance(data, bytes) else data}_for_{recipient_public_key[:8]}"
    
    # Real seal
    public_key = nacl.public.PublicKey(recipient_public_key, encoder=nacl.encoding.HexEncoder)
    sealed_box = nacl.public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(data)
    
    return nacl.encoding.HexEncoder.encode(encrypted).decode()


def unseal(sealed_data, private_key, public_key=None):
    """
    Unseal data with private key.
    Args:
        sealed_data: hex-encoded sealed box
        private_key: hex-encoded private key
        public_key: optional hex-encoded public key (for optimization)
    Returns:
        unsealed bytes or None if unsealing fails
    """
    if get_crypto_mode() == "dummy":
        # Dummy unseal
        if isinstance(sealed_data, str) and sealed_data.startswith("dummy_sealed_"):
            parts = sealed_data.split("_for_")
            if len(parts) > 1:
                return parts[0][len("dummy_sealed_"):].encode()
        return None
    
    # Real unseal
    try:
        private_key_obj = nacl.signing.SigningKey(private_key, encoder=nacl.encoding.HexEncoder)
        keypair = nacl.public.Box(private_key_obj.to_curve25519_private_key(), private_key_obj.to_curve25519_private_key().public_key)
        sealed_box = nacl.public.SealedBox(keypair)
        
        encrypted = nacl.encoding.HexEncoder.decode(sealed_data)
        return sealed_box.decrypt(encrypted)
    except:
        return None


def kdf(password, salt=None, ops_limit=None, mem_limit=None):
    """
    Key derivation function (KDF) using Argon2id.
    Args:
        password: password string or bytes
        salt: optional salt (generated if not provided)
        ops_limit: CPU operations limit
        mem_limit: memory limit
    Returns:
        dict with derived_key (hex), salt (hex), and algorithm
    """
    if isinstance(password, str):
        password = password.encode()
    
    if get_crypto_mode() == "dummy":
        # Dummy KDF
        dummy_salt = salt or b"dummy_salt"
        if isinstance(dummy_salt, str):
            dummy_salt = dummy_salt.encode()
        derived = hashlib.blake2b(password + dummy_salt, digest_size=32).digest()
        return {
            "derived_key": derived.hex(),
            "salt": dummy_salt.hex(),
            "algorithm": "dummy_kdf"
        }
    
    # Real KDF using Argon2id
    if salt is None:
        salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
    elif isinstance(salt, str):
        salt = nacl.encoding.HexEncoder.decode(salt)
    
    ops_limit = ops_limit or nacl.pwhash.argon2id.OPSLIMIT_MODERATE
    mem_limit = mem_limit or nacl.pwhash.argon2id.MEMLIMIT_MODERATE
    
    derived = nacl.pwhash.argon2id.kdf(
        nacl.secret.SecretBox.KEY_SIZE,
        password,
        salt,
        opslimit=ops_limit,
        memlimit=mem_limit
    )
    
    return {
        "derived_key": nacl.encoding.HexEncoder.encode(derived).decode(),
        "salt": nacl.encoding.HexEncoder.encode(salt).decode(),
        "algorithm": "argon2id"
    }