"""Crypto bridge module for gradual Python → Rust migration.

This module provides a unified interface that can use either the Python
implementation (nacl-based) or the Rust implementation (quiet_core).

Usage:
    # In code that uses crypto:
    from core.crypto_bridge import verify, sign, hash  # Instead of core.crypto

    # The bridge automatically uses Rust if available, falls back to Python

Configuration:
    Set environment variable QUIET_USE_RUST=1 to force Rust implementation
    Set environment variable QUIET_USE_PYTHON=1 to force Python implementation

Migration status:
    ✓ verify         - Rust implementation ready
    ✓ sign           - Rust implementation ready
    ✓ hash           - Rust implementation ready
    ✓ kdf            - Rust implementation ready
    ✓ b64encode      - Rust implementation ready
    ✓ b64decode      - Rust implementation ready
    ✓ verify_event   - Rust implementation ready
    ✓ sign_event     - Rust implementation ready
    ○ generate_keypair - Rust implementation ready (but uses Python for compatibility)
    ○ encrypt/decrypt  - TODO: migrate
    ○ seal/unseal      - TODO: migrate
    ○ wrap/unwrap      - TODO: migrate
"""

import os
import json
import logging
from typing import Any, Tuple

log = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

_USE_RUST = os.environ.get('QUIET_USE_RUST', '0') == '1'
_USE_PYTHON = os.environ.get('QUIET_USE_PYTHON', '0') == '1'

# Try to import Rust implementation
_rust_available = False
try:
    import quiet_core as _rust
    _rust_available = True
    log.info("Rust crypto implementation available")
except ImportError:
    log.info("Rust crypto not available, using Python implementation")

# Determine which implementation to use
def _should_use_rust() -> bool:
    if _USE_PYTHON:
        return False
    if _USE_RUST:
        return _rust_available
    # Default: use Rust if available
    return _rust_available


# ============================================================================
# Python Implementation (fallback)
# ============================================================================

from core import crypto as _python_crypto


# ============================================================================
# Bridged Functions
# ============================================================================

def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Verify an Ed25519 signature.

    Implementation: Rust (quiet_core.verify) or Python (nacl)
    Verus status: verified (Rust), trusted (Python)
    """
    if _should_use_rust():
        return _rust.verify(message, signature, public_key)
    else:
        return _python_crypto.verify(message, signature, public_key)


def sign(message: bytes, private_key: bytes) -> bytes:
    """Sign a message with Ed25519.

    Implementation: Rust (quiet_core.sign) or Python (nacl)
    Verus status: trusted (both - uses ring/nacl)
    """
    if _should_use_rust():
        return bytes(_rust.sign(message, private_key))
    else:
        return _python_crypto.sign(message, private_key)


def hash(data: bytes, size: int = 16) -> bytes:
    """BLAKE2b hash with configurable output size.

    Implementation: Rust (quiet_core.hash) or Python (nacl)
    Verus status: verified (Rust), trusted (Python)
    """
    if _should_use_rust():
        return bytes(_rust.hash(data, size))
    else:
        return _python_crypto.hash(data, size)


def kdf(secret: bytes, salt: bytes, size: int = 32) -> bytes:
    """Key derivation function using BLAKE2b with salt.

    Implementation: Rust (quiet_core.kdf) or Python (nacl)
    Verus status: verified (Rust), trusted (Python)
    """
    if _should_use_rust():
        return bytes(_rust.kdf(secret, salt, size))
    else:
        return _python_crypto.kdf(secret, salt, size)


def b64encode(data: bytes) -> str:
    """Encode bytes to base64 string.

    Implementation: Rust or Python (both verified/trivial)
    """
    if _should_use_rust():
        return _rust.b64encode(data)
    else:
        return _python_crypto.b64encode(data)


def b64decode(data: str) -> bytes:
    """Decode base64 string to bytes.

    Implementation: Rust or Python (both verified/trivial)
    """
    if _should_use_rust():
        return bytes(_rust.b64decode(data))
    else:
        return _python_crypto.b64decode(data)


def verify_event(event_data: dict[str, Any], public_key: bytes) -> bool:
    """Verify event signature.

    Implementation: Rust (quiet_core.verify_event) or Python
    Verus status: verified (Rust)
    """
    if _should_use_rust():
        json_str = json.dumps(event_data, sort_keys=True, separators=(',', ':'))
        return _rust.verify_event(json_str, public_key)
    else:
        return _python_crypto.verify_event(event_data, public_key)


def sign_event(event_data: dict[str, Any], private_key: bytes) -> dict[str, Any]:
    """Sign an event dict, returning dict with signature added.

    Implementation: Rust (quiet_core.sign_event) or Python
    Verus status: verified (Rust)
    """
    if _should_use_rust():
        json_str = json.dumps(event_data, sort_keys=True, separators=(',', ':'))
        result_json = _rust.sign_event(json_str, private_key)
        return json.loads(result_json)
    else:
        return _python_crypto.sign_event(event_data, private_key)


def canonicalize_json(obj: dict[str, Any]) -> bytes:
    """Canonicalize a JSON object to bytes for deterministic operations.

    Implementation: Rust or Python (both verified/deterministic)
    """
    if _should_use_rust():
        json_str = json.dumps(obj)
        return bytes(_rust.canonicalize_json(json_str))
    else:
        return _python_crypto.canonicalize_json(obj)


# ============================================================================
# Functions still using Python (not yet migrated)
# ============================================================================

# These functions pass through to Python implementation
# They will be migrated incrementally

def generate_keypair() -> Tuple[bytes, bytes]:
    """Generate an Ed25519 keypair.

    Status: Using Python for now (key format compatibility)
    """
    return _python_crypto.generate_keypair()


def generate_secret() -> bytes:
    """Generate a random 32-byte secret.

    Status: Using Python (RNG)
    """
    return _python_crypto.generate_secret()


def encrypt(plaintext: bytes, key: bytes, nonce: bytes) -> bytes:
    """Encrypt with XChaCha20-Poly1305.

    Status: TODO migrate to Rust
    """
    return _python_crypto.encrypt(plaintext, key, nonce)


def decrypt(ciphertext: bytes, key: bytes, nonce: bytes) -> bytes:
    """Decrypt with XChaCha20-Poly1305.

    Status: TODO migrate to Rust
    """
    return _python_crypto.decrypt(ciphertext, key, nonce)


def seal(plaintext: bytes, public_key: bytes) -> bytes:
    """Deterministically seal a message to a public key.

    Status: TODO migrate to Rust
    """
    return _python_crypto.seal(plaintext, public_key)


def unseal(sealed_data: bytes, private_key: bytes) -> bytes:
    """Unseal a sealed message.

    Status: TODO migrate to Rust
    """
    return _python_crypto.unseal(sealed_data, private_key)


# Pass through all other functions unchanged
wrap = _python_crypto.wrap
unwrap = _python_crypto.unwrap
unwrap_event = _python_crypto.unwrap_event
unwrap_transit = _python_crypto.unwrap_transit
verify_signed_by_peer_shared = _python_crypto.verify_signed_by_peer_shared
deterministic_nonce = _python_crypto.deterministic_nonce
get_key_by_id = _python_crypto.get_key_by_id
get_event_key_by_id = _python_crypto.get_event_key_by_id
get_transit_key_by_id = _python_crypto.get_transit_key_by_id
encrypt_file_slice = _python_crypto.encrypt_file_slice
decrypt_file_slice = _python_crypto.decrypt_file_slice
compute_file_id = _python_crypto.compute_file_id
compute_root_hash = _python_crypto.compute_root_hash
derive_slice_nonce = _python_crypto.derive_slice_nonce
HashBuilder = _python_crypto.HashBuilder

# Constants
HINT_SIZE = _python_crypto.HINT_SIZE
SECRET_SIZE = _python_crypto.SECRET_SIZE
NONCE_SIZE = _python_crypto.NONCE_SIZE


# ============================================================================
# Migration Helpers
# ============================================================================

def get_implementation_status() -> dict[str, str]:
    """Return status of each function's implementation.

    Useful for tracking migration progress.
    """
    return {
        'verify': 'rust' if _should_use_rust() else 'python',
        'sign': 'rust' if _should_use_rust() else 'python',
        'hash': 'rust' if _should_use_rust() else 'python',
        'kdf': 'rust' if _should_use_rust() else 'python',
        'b64encode': 'rust' if _should_use_rust() else 'python',
        'b64decode': 'rust' if _should_use_rust() else 'python',
        'verify_event': 'rust' if _should_use_rust() else 'python',
        'sign_event': 'rust' if _should_use_rust() else 'python',
        'canonicalize_json': 'rust' if _should_use_rust() else 'python',
        'generate_keypair': 'python',
        'generate_secret': 'python',
        'encrypt': 'python',
        'decrypt': 'python',
        'seal': 'python',
        'unseal': 'python',
        'wrap': 'python',
        'unwrap': 'python',
    }


def is_rust_available() -> bool:
    """Check if Rust implementation is available."""
    return _rust_available


def using_rust() -> bool:
    """Check if currently using Rust implementation."""
    return _should_use_rust()
