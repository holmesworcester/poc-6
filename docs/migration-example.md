# Migration Example: Python → Rust

This document shows concrete examples of migrating functions from Python to Rust.

## Step-by-Step Migration

### Step 1: Identify the Function

We'll migrate `verify()` from `core/crypto.py`:

```python
# core/crypto.py (BEFORE)
def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Verify an Ed25519 signature."""
    try:
        verify_key = nacl.signing.VerifyKey(public_key)
        verify_key.verify(message, signature)
        return True
    except nacl.exceptions.BadSignatureError:
        return False
```

### Step 2: Write the Rust Implementation

```rust
// rust/src/crypto.rs
/// Verify an Ed25519 signature.
///
/// # Verus Status: verified
///
/// # Specification
/// ```text
/// requires: public_key.len() == 32
/// requires: signature.len() == 64
/// ensures: result == ed25519_valid(public_key, message, signature)
/// ```
pub fn verify(message: &[u8], signature: &[u8], public_key: &[u8]) -> bool {
    if public_key.len() != PUBLIC_KEY_SIZE {
        return false;
    }
    if signature.len() != SIGNATURE_SIZE {
        return false;
    }

    let peer_public_key = UnparsedPublicKey::new(&ED25519, public_key);
    peer_public_key.verify(message, signature).is_ok()
}
```

### Step 3: Add PyO3 Binding

```rust
// rust/src/lib.rs
#[pyfunction]
fn verify(message: &[u8], signature: &[u8], public_key: &[u8]) -> bool {
    crypto::verify(message, signature, public_key)
}
```

### Step 4: Create the Bridge

```python
# core/crypto_bridge.py
def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    if _should_use_rust():
        return _rust.verify(message, signature, public_key)
    else:
        return _python_crypto.verify(message, signature, public_key)
```

### Step 5: Update Imports (One Line Change)

```python
# BEFORE: events/network/recorded.py
from core import crypto
# ...
if not crypto.verify(canonical, signature, public_key):

# AFTER: events/network/recorded.py
from core import crypto_bridge as crypto  # Only this line changes!
# ...
if not crypto.verify(canonical, signature, public_key):  # Same API
```

### Step 6: Run Existing Tests

```bash
# Build Rust library
cd rust && maturin develop

# Run existing Python tests - they are the oracle
cd .. && PYTHONPATH=. pytest tests/ -v

# All tests should pass with no changes
```

## Full Example: Migrating verify_event

### Python Original

```python
# core/crypto.py
def verify_event(event_data: dict[str, Any], public_key: bytes) -> bool:
    """Verify event signature."""
    event_type = event_data.get('type', 'unknown')
    sig_b64 = event_data.get('signature')

    if not sig_b64:
        return False

    event_without_sig = {k: v for k, v in event_data.items() if k != 'signature'}
    canonical = canonicalize_json(event_without_sig)

    try:
        return verify(canonical, b64decode(sig_b64), public_key)
    except Exception:
        return False
```

### Rust Implementation

```rust
// rust/src/crypto.rs
pub fn verify_event(event_data: &serde_json::Value, public_key: &[u8]) -> bool {
    let obj = match event_data {
        serde_json::Value::Object(map) => map,
        _ => return false,
    };

    // Get signature
    let sig_b64 = match obj.get("signature") {
        Some(serde_json::Value::String(s)) => s,
        _ => return false,
    };

    let signature = match b64decode(sig_b64) {
        Ok(s) => s,
        Err(_) => return false,
    };

    // Remove signature for verification
    let mut event_without_sig = obj.clone();
    event_without_sig.remove("signature");

    let canonical = canonicalize_json(&serde_json::Value::Object(event_without_sig));

    verify(&canonical, &signature, public_key)
}
```

### PyO3 Binding

```rust
// rust/src/lib.rs
#[pyfunction]
fn verify_event(json_str: &str, public_key: &[u8]) -> PyResult<bool> {
    let value: serde_json::Value = serde_json::from_str(json_str)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(crypto::verify_event(&value, public_key))
}
```

### Bridge

```python
# core/crypto_bridge.py
def verify_event(event_data: dict[str, Any], public_key: bytes) -> bool:
    if _should_use_rust():
        json_str = json.dumps(event_data, sort_keys=True, separators=(',', ':'))
        return _rust.verify_event(json_str, public_key)
    else:
        return _python_crypto.verify_event(event_data, public_key)
```

## Migration Order

### Phase 1: Pure Crypto (No State)

```
Week 1-2:
├── verify           ✓ Rust ready
├── sign             ✓ Rust ready
├── hash             ✓ Rust ready
├── kdf              ✓ Rust ready
├── b64encode        ✓ Rust ready
├── b64decode        ✓ Rust ready
├── verify_event     ✓ Rust ready
└── sign_event       ✓ Rust ready
```

### Phase 2: Encryption

```
Week 3-4:
├── encrypt
├── decrypt
├── seal
├── unseal
└── deterministic_nonce
```

### Phase 3: Key Management (Needs DB)

```
Week 5-6:
├── get_key_by_id
├── get_event_key_by_id
├── get_transit_key_by_id
├── wrap
└── unwrap
```

### Phase 4: File Operations

```
Week 7-8:
├── encrypt_file_slice
├── decrypt_file_slice
├── compute_file_id
└── compute_root_hash
```

## Verification Priorities

Not every function needs Verus proofs immediately. Prioritize:

### High Priority (Verify First)

1. **verify** - Authentication boundary
2. **verify_event** - Event integrity
3. **parse_event_envelope** - Input validation
4. **check_deps** - Authorization logic

### Medium Priority

5. **sign/sign_event** - Ensure correct signing
6. **hash** - Determinism
7. **canonicalize_json** - Determinism

### Lower Priority (Trusted Primitives)

8. **encrypt/decrypt** - Wraps ring (audited)
9. **seal/unseal** - Wraps ring (audited)
10. **generate_keypair** - Uses ring RNG

## Testing the Migration

### Unit Tests

```python
# tests/test_crypto_bridge.py
import pytest
from core import crypto_bridge as crypto

def test_verify_roundtrip():
    private_key, public_key = crypto.generate_keypair()
    message = b"hello world"
    signature = crypto.sign(message, private_key)

    assert crypto.verify(message, signature, public_key)
    assert not crypto.verify(b"wrong", signature, public_key)

def test_rust_python_equivalence():
    """Verify Rust and Python produce identical results."""
    import os

    private_key, public_key = crypto.generate_keypair()
    message = b"test message"

    # Test with Python
    os.environ['QUIET_USE_PYTHON'] = '1'
    os.environ['QUIET_USE_RUST'] = '0'
    sig_python = crypto.sign(message, private_key)

    # Test with Rust
    os.environ['QUIET_USE_PYTHON'] = '0'
    os.environ['QUIET_USE_RUST'] = '1'
    sig_rust = crypto.sign(message, private_key)

    # Both should produce valid signatures
    assert crypto.verify(message, sig_python, public_key)
    assert crypto.verify(message, sig_rust, public_key)

    # Signatures should be identical (deterministic)
    assert sig_python == sig_rust
```

### Integration Tests

```bash
# Run full test suite with Rust
QUIET_USE_RUST=1 PYTHONPATH=. pytest tests/ -v

# Run full test suite with Python (baseline)
QUIET_USE_PYTHON=1 PYTHONPATH=. pytest tests/ -v

# Both should pass with identical results
```

## Build Commands

```bash
# Development build (fast, includes debug info)
cd rust && maturin develop

# Release build (optimized)
cd rust && maturin develop --release

# Build wheel for distribution
cd rust && maturin build --release

# Run Rust tests
cd rust && cargo test

# Run Verus verification (when specs are added)
cd rust && verus src/crypto.rs
```

## Troubleshooting

### Import Error

```
ImportError: No module named 'quiet_core'
```

Solution: Build the Rust library first:
```bash
cd rust && maturin develop
```

### Signature Mismatch

If Rust and Python produce different signatures, check:
1. Key format (seed vs PKCS8)
2. Endianness
3. Canonicalization

### Performance

If Rust is slower than Python:
1. Use `maturin develop --release`
2. Check for unnecessary copies at FFI boundary
3. Profile with `py-spy`
