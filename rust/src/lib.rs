//! Quiet Protocol Core Library
//!
//! This crate provides verified implementations of the Quiet protocol primitives,
//! exposed to Python via PyO3.
//!
//! # Module Structure
//!
//! - `crypto`: Cryptographic primitives (signatures, hashing, encryption)
//! - `parser`: Event wire format parsers (nom-based)
//! - `events`: Event projection logic
//!
//! # Verification Status
//!
//! Functions are progressively verified using Verus. Check individual module
//! documentation for verification status of each function.

use pyo3::prelude::*;
use pyo3::exceptions::{PyValueError, PyRuntimeError};

pub mod crypto;
pub mod parser;

// ============================================================================
// Python Bindings for Crypto
// ============================================================================

/// Generate an Ed25519 keypair.
///
/// Returns (private_key, public_key) as bytes.
#[pyfunction]
fn generate_keypair(py: Python<'_>) -> PyResult<(Py<pyo3::types::PyBytes>, Py<pyo3::types::PyBytes>)> {
    let (private_key, public_key) = crypto::generate_keypair()
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;

    Ok((
        pyo3::types::PyBytes::new_bound(py, &private_key).into(),
        pyo3::types::PyBytes::new_bound(py, &public_key).into(),
    ))
}

/// Sign a message with Ed25519.
///
/// Args:
///     message: The message to sign (bytes)
///     private_key: 32-byte Ed25519 private key
///
/// Returns:
///     64-byte signature
#[pyfunction]
fn sign(py: Python<'_>, message: &[u8], private_key: &[u8]) -> PyResult<Py<pyo3::types::PyBytes>> {
    let signature = crypto::sign(message, private_key)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;

    Ok(pyo3::types::PyBytes::new_bound(py, &signature).into())
}

/// Verify an Ed25519 signature.
///
/// Args:
///     message: The message that was signed
///     signature: 64-byte signature
///     public_key: 32-byte public key
///
/// Returns:
///     True if signature is valid, False otherwise
#[pyfunction]
fn verify(message: &[u8], signature: &[u8], public_key: &[u8]) -> bool {
    crypto::verify(message, signature, public_key)
}

/// BLAKE2b hash with configurable size.
///
/// Args:
///     data: Data to hash
///     size: Output size in bytes (default: 16)
///
/// Returns:
///     Hash digest
#[pyfunction]
#[pyo3(signature = (data, size=16))]
fn hash(py: Python<'_>, data: &[u8], size: usize) -> Py<pyo3::types::PyBytes> {
    let h = crypto::hash(data, size);
    pyo3::types::PyBytes::new_bound(py, &h).into()
}

/// Key derivation function.
///
/// Args:
///     secret: Secret input
///     salt: Salt value
///     size: Output size in bytes (default: 32)
///
/// Returns:
///     Derived key
#[pyfunction]
#[pyo3(signature = (secret, salt, size=32))]
fn kdf(py: Python<'_>, secret: &[u8], salt: &[u8], size: usize) -> Py<pyo3::types::PyBytes> {
    let derived = crypto::kdf(secret, salt, size);
    pyo3::types::PyBytes::new_bound(py, &derived).into()
}

/// Encode bytes to base64 string.
#[pyfunction]
fn b64encode(data: &[u8]) -> String {
    crypto::b64encode(data)
}

/// Decode base64 string to bytes.
#[pyfunction]
fn b64decode(py: Python<'_>, data: &str) -> PyResult<Py<pyo3::types::PyBytes>> {
    let decoded = crypto::b64decode(data)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(pyo3::types::PyBytes::new_bound(py, &decoded).into())
}

/// Canonicalize JSON for deterministic signing.
#[pyfunction]
fn canonicalize_json(py: Python<'_>, json_str: &str) -> PyResult<Py<pyo3::types::PyBytes>> {
    let value: serde_json::Value = serde_json::from_str(json_str)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let canonical = crypto::canonicalize_json(&value);
    Ok(pyo3::types::PyBytes::new_bound(py, &canonical).into())
}

/// Sign an event dict, returning dict with signature added.
#[pyfunction]
fn sign_event(json_str: &str, private_key: &[u8]) -> PyResult<String> {
    let value: serde_json::Value = serde_json::from_str(json_str)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;

    let signed = crypto::sign_event(&value, private_key)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;

    serde_json::to_string(&signed)
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

/// Verify an event signature.
#[pyfunction]
fn verify_event(json_str: &str, public_key: &[u8]) -> PyResult<bool> {
    let value: serde_json::Value = serde_json::from_str(json_str)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;

    Ok(crypto::verify_event(&value, public_key))
}

// ============================================================================
// Python Bindings for Parser
// ============================================================================

/// Parse an event from wire format bytes.
///
/// Returns parsed event as JSON string, or raises ValueError if invalid.
#[pyfunction]
fn parse_event(data: &[u8]) -> PyResult<String> {
    let event = parser::parse_event_envelope(data)
        .map_err(|e| PyValueError::new_err(format!("Parse error: {}", e)))?;

    serde_json::to_string(&event)
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

/// Validate event structure without full parsing.
///
/// Returns True if event has valid structure, False otherwise.
#[pyfunction]
fn validate_event_structure(data: &[u8]) -> bool {
    parser::validate_event_structure(data)
}

// ============================================================================
// Python Module Definition
// ============================================================================

/// Quiet Protocol Core - Verified implementation
#[pymodule]
fn quiet_core(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Crypto functions
    m.add_function(wrap_pyfunction!(generate_keypair, m)?)?;
    m.add_function(wrap_pyfunction!(sign, m)?)?;
    m.add_function(wrap_pyfunction!(verify, m)?)?;
    m.add_function(wrap_pyfunction!(hash, m)?)?;
    m.add_function(wrap_pyfunction!(kdf, m)?)?;
    m.add_function(wrap_pyfunction!(b64encode, m)?)?;
    m.add_function(wrap_pyfunction!(b64decode, m)?)?;
    m.add_function(wrap_pyfunction!(canonicalize_json, m)?)?;
    m.add_function(wrap_pyfunction!(sign_event, m)?)?;
    m.add_function(wrap_pyfunction!(verify_event, m)?)?;

    // Parser functions
    m.add_function(wrap_pyfunction!(parse_event, m)?)?;
    m.add_function(wrap_pyfunction!(validate_event_structure, m)?)?;

    // Constants
    m.add("HINT_SIZE", crypto::HINT_SIZE)?;
    m.add("SECRET_SIZE", crypto::SECRET_SIZE)?;
    m.add("NONCE_SIZE", crypto::NONCE_SIZE)?;
    m.add("SIGNATURE_SIZE", crypto::SIGNATURE_SIZE)?;
    m.add("PUBLIC_KEY_SIZE", crypto::PUBLIC_KEY_SIZE)?;
    m.add("PRIVATE_KEY_SIZE", crypto::PRIVATE_KEY_SIZE)?;

    Ok(())
}
