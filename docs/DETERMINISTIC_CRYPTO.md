# Deterministic Encryption in Content-Addressed Systems

This document explains why and how we use deterministic encryption in this codebase.

## The Key Insight

In a content-addressed system:
```
event_id = hash(blob)
```

This means **identical content produces identical event IDs**, which are then deduplicated. This fundamentally changes the security model for encryption nonces.

## Traditional vs Content-Addressed Encryption

### Traditional Systems

In traditional systems, random nonces serve two purposes:
1. **Hide identical plaintexts** - Same message twice should produce different ciphertexts
2. **Prevent key stream reuse** - Required by stream ciphers for security

So Alice sending "hello" twice produces:
```
ciphertext1 = encrypt("hello", key, random_nonce1)  # Different each time
ciphertext2 = encrypt("hello", key, random_nonce2)
```

### Content-Addressed Systems

In our system, if Alice creates the same event twice:
```
blob1 = encrypt("hello", key, nonce)
blob2 = encrypt("hello", key, nonce)  # If deterministic: blob1 == blob2

event_id1 = hash(blob1)
event_id2 = hash(blob2)  # If blob1 == blob2: event_id1 == event_id2

# Result: It's the SAME EVENT, deduplicated!
```

The "attack" that random nonces prevent (detecting identical plaintexts) is **impossible** in content-addressed systems because identical ciphertexts produce identical event IDs, which are the same event.

## Why Random Nonces Don't Help

Consider the scenarios:

| Scenario | Random Nonce | Deterministic Nonce |
|----------|--------------|---------------------|
| Same content twice | Two events, both get same deduplicated ID | One event |
| Different content | Different events | Different events |
| Replay attack | Same event_id, deduplicated | Same event_id, deduplicated |

The random nonce provides **no additional security** because the event_id deduplication already prevents the attacks it would defend against.

## Our Implementation

### Symmetric Encryption
```python
def wrap(plaintext, key):
    # Nonce derived from content - deterministic
    nonce = hash(key_id + plaintext)[:24]
    return encrypt(plaintext, key, nonce)
```

Same plaintext + same key → same ciphertext → same event_id → same event.

### Asymmetric Encryption (seal/unseal)
```python
def seal(plaintext, recipient_public_key):
    # Derive ephemeral keypair deterministically
    seed = hash(recipient_public_key + plaintext)[:32]
    ephemeral_private = PrivateKey(seed)

    # Derive nonce from plaintext
    nonce = hash(plaintext)[:24]

    # Encrypt
    box = Box(ephemeral_private, recipient_public_key)
    return ephemeral_public + nonce + box.encrypt(plaintext, nonce)
```

Same plaintext + same recipient → same ciphertext → same event_id → same event.

## Security Analysis

### What We Preserve
- **Confidentiality** - Content is still encrypted
- **Authentication** - Events are still signed
- **Integrity** - Ciphertext tampering detected by auth tag
- **Replay Protection** - Event deduplication by content-addressed ID

### What We Trade Off
- **Forward Secrecy on Ephemeral Keys** - Traditional SealedBox generates random ephemeral keys, providing forward secrecy even if long-term keys are compromised. Our deterministic version derives ephemeral keys from content, so compromising long-term keys allows decrypting past messages IF you have the ciphertext.

This tradeoff is acceptable because:
1. The symmetric keys being shared rotate frequently (group key rotation)
2. The primary forward secrecy comes from symmetric key rotation, not ephemeral asymmetric keys
3. We gain reproducible event creation, enabling pure functional patterns

## Benefits

1. **Pure Functional Event Creation** - `create_pure(deps) → CreateResult` is fully deterministic
2. **Testability** - Same inputs always produce same outputs
3. **Content Deduplication** - Identical events are naturally deduplicated
4. **Reproducible Builds** - Event creation can be verified/reproduced

## References

- Content-addressed storage: Events are identified by `hash(content)`
- NaCl Box: X25519 key agreement + XSalsa20-Poly1305 authenticated encryption
- Deterministic nonce derivation: `hash(inputs)[:nonce_size]`
