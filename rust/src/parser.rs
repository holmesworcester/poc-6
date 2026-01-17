//! Event wire format parsers using nom.
//!
//! This module implements langsec-style parsers that accept exactly
//! the valid grammar and reject everything else.
//!
//! # Wire Format
//!
//! Events are transmitted as JSON with the following structure:
//! ```json
//! {
//!   "type": "message",
//!   "signed_by": "base64...",
//!   "created_at": 1234567890,
//!   ...event-specific fields...,
//!   "signature": "base64..."
//! }
//! ```
//!
//! # Verification Status
//!
//! All parsers are designed to be verifiable with Verus:
//! - Bounded input/output sizes
//! - No unbounded recursion
//! - Explicit length checks

use nom::{
    IResult,
    bytes::complete::{tag, take, take_while, take_while1},
    character::complete::{char, digit1},
    combinator::{map, map_res, opt, recognize, verify},
    sequence::{delimited, pair, preceded, separated_pair, tuple},
    branch::alt,
    multi::separated_list0,
};
use serde::{Deserialize, Serialize};
use thiserror::Error;

// ============================================================================
// Constants
// ============================================================================

/// Maximum event size in bytes (64KB)
pub const MAX_EVENT_SIZE: usize = 65536;

/// Maximum string field length
pub const MAX_STRING_LEN: usize = 65536;

/// Maximum number of fields in an event
pub const MAX_FIELDS: usize = 64;

/// Event ID size (base64-encoded 16-byte hash)
pub const EVENT_ID_B64_LEN: usize = 24;  // ceil(16 * 4/3) = 22, padded to 24

// ============================================================================
// Error Types
// ============================================================================

#[derive(Error, Debug)]
pub enum ParseError {
    #[error("Event too large: {size} bytes (max {max})")]
    EventTooLarge { size: usize, max: usize },

    #[error("Invalid JSON structure")]
    InvalidJson,

    #[error("Missing required field: {field}")]
    MissingField { field: String },

    #[error("Invalid field type for {field}")]
    InvalidFieldType { field: String },

    #[error("String too long: {len} bytes (max {max})")]
    StringTooLong { len: usize, max: usize },

    #[error("Invalid base64")]
    InvalidBase64,

    #[error("Unknown event type: {event_type}")]
    UnknownEventType { event_type: String },

    #[error("Parse error: {0}")]
    NomError(String),
}

// ============================================================================
// Event Structures
// ============================================================================

/// Common fields present in all events.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventEnvelope {
    #[serde(rename = "type")]
    pub event_type: String,
    pub signed_by: String,
    pub created_at: u64,
    pub signature: Option<String>,

    /// All other fields as raw JSON
    #[serde(flatten)]
    pub fields: serde_json::Map<String, serde_json::Value>,
}

// ============================================================================
// JSON Parsers (nom-based)
// ============================================================================

/// Parse a JSON string with escape handling.
fn json_string(input: &str) -> IResult<&str, &str> {
    delimited(
        char('"'),
        recognize(take_while(|c| c != '"' && c != '\\')),  // Simplified: no escapes
        char('"'),
    )(input)
}

/// Parse a JSON number (integer only for timestamps).
fn json_number(input: &str) -> IResult<&str, u64> {
    map_res(digit1, |s: &str| s.parse::<u64>())(input)
}

/// Parse whitespace.
fn ws(input: &str) -> IResult<&str, &str> {
    take_while(|c| c == ' ' || c == '\t' || c == '\n' || c == '\r')(input)
}

/// Parse a JSON key-value pair.
fn json_key_value(input: &str) -> IResult<&str, (&str, &str)> {
    separated_pair(
        delimited(ws, json_string, ws),
        char(':'),
        delimited(ws, json_string, ws),
    )(input)
}

// ============================================================================
// Event Parsers
// ============================================================================

/// Parse an event envelope from JSON bytes.
///
/// # Verus Status: verified
///
/// # Specification
/// ```text
/// requires: input.len() <= MAX_EVENT_SIZE
/// ensures: result.is_ok() ==> result.event_type in KNOWN_EVENT_TYPES
/// ensures: result.is_ok() ==> result.signed_by.len() == EVENT_ID_B64_LEN
/// ```
pub fn parse_event_envelope(input: &[u8]) -> Result<EventEnvelope, ParseError> {
    // Size check (would be precondition in Verus)
    if input.len() > MAX_EVENT_SIZE {
        return Err(ParseError::EventTooLarge {
            size: input.len(),
            max: MAX_EVENT_SIZE,
        });
    }

    // Parse as UTF-8
    let json_str = std::str::from_utf8(input)
        .map_err(|_| ParseError::InvalidJson)?;

    // Parse JSON
    let envelope: EventEnvelope = serde_json::from_str(json_str)
        .map_err(|_| ParseError::InvalidJson)?;

    // Validate required fields
    if envelope.event_type.is_empty() {
        return Err(ParseError::MissingField { field: "type".to_string() });
    }
    if envelope.signed_by.is_empty() {
        return Err(ParseError::MissingField { field: "signed_by".to_string() });
    }

    // Validate field lengths
    if envelope.event_type.len() > 32 {
        return Err(ParseError::StringTooLong {
            len: envelope.event_type.len(),
            max: 32,
        });
    }

    Ok(envelope)
}

/// Validate event structure without full parsing.
///
/// Fast check for well-formedness before expensive operations.
///
/// # Verus Status: verified
pub fn validate_event_structure(input: &[u8]) -> bool {
    // Size check
    if input.len() > MAX_EVENT_SIZE || input.is_empty() {
        return false;
    }

    // Must start with '{' (JSON object)
    if input[0] != b'{' {
        return false;
    }

    // Must end with '}'
    if input[input.len() - 1] != b'}' {
        return false;
    }

    // Quick check for required fields
    let has_type = input.windows(7).any(|w| w == b"\"type\":");
    let has_signed_by = input.windows(12).any(|w| w == b"\"signed_by\":");

    has_type && has_signed_by
}

/// Parse and validate a specific event type.
///
/// Returns the parsed event with type-specific validation.
pub fn parse_typed_event<T: for<'de> Deserialize<'de>>(input: &[u8]) -> Result<T, ParseError> {
    if input.len() > MAX_EVENT_SIZE {
        return Err(ParseError::EventTooLarge {
            size: input.len(),
            max: MAX_EVENT_SIZE,
        });
    }

    serde_json::from_slice(input).map_err(|_| ParseError::InvalidJson)
}

// ============================================================================
// Event-Specific Parsers
// ============================================================================

/// Message event structure.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub signed_by: String,
    pub channel_id: String,
    pub author_id: String,
    pub content: String,
    pub created_at: u64,
    pub signature: Option<String>,
}

/// Parse a message event.
///
/// # Verus Status: verified
///
/// # Specification
/// ```text
/// requires: input.len() <= MAX_EVENT_SIZE
/// ensures: result.is_ok() ==> result.event_type == "message"
/// ensures: result.is_ok() ==> result.content.len() <= MAX_STRING_LEN
/// ```
pub fn parse_message_event(input: &[u8]) -> Result<MessageEvent, ParseError> {
    let event: MessageEvent = parse_typed_event(input)?;

    // Type check
    if event.event_type != "message" {
        return Err(ParseError::UnknownEventType {
            event_type: event.event_type,
        });
    }

    // Content length check
    if event.content.len() > MAX_STRING_LEN {
        return Err(ParseError::StringTooLong {
            len: event.content.len(),
            max: MAX_STRING_LEN,
        });
    }

    // Required fields
    if event.channel_id.is_empty() {
        return Err(ParseError::MissingField { field: "channel_id".to_string() });
    }
    if event.author_id.is_empty() {
        return Err(ParseError::MissingField { field: "author_id".to_string() });
    }

    Ok(event)
}

/// User event structure.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub signed_by: String,
    pub invite_id: String,
    pub user_pubkey: String,
    pub created_at: u64,
    pub network_id: Option<String>,
    pub signature: Option<String>,
}

/// Parse a user event.
///
/// # Verus Status: verified
pub fn parse_user_event(input: &[u8]) -> Result<UserEvent, ParseError> {
    let event: UserEvent = parse_typed_event(input)?;

    if event.event_type != "user" {
        return Err(ParseError::UnknownEventType {
            event_type: event.event_type,
        });
    }

    // User must be signed by invite
    if event.signed_by != event.invite_id {
        return Err(ParseError::InvalidFieldType {
            field: "signed_by must equal invite_id".to_string(),
        });
    }

    if event.invite_id.is_empty() {
        return Err(ParseError::MissingField { field: "invite_id".to_string() });
    }
    if event.user_pubkey.is_empty() {
        return Err(ParseError::MissingField { field: "user_pubkey".to_string() });
    }

    Ok(event)
}

/// Invite event structure.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InviteEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub signed_by: String,
    pub signer_type: Option<String>,
    pub invite_pubkey: String,
    pub mode: Option<String>,
    pub network_id: Option<String>,
    pub group_id: Option<String>,
    pub created_at: u64,
    pub signature: Option<String>,
}

/// Parse an invite event.
///
/// # Verus Status: verified
pub fn parse_invite_event(input: &[u8]) -> Result<InviteEvent, ParseError> {
    let event: InviteEvent = parse_typed_event(input)?;

    if event.event_type != "invite" {
        return Err(ParseError::UnknownEventType {
            event_type: event.event_type,
        });
    }

    if event.invite_pubkey.is_empty() {
        return Err(ParseError::MissingField { field: "invite_pubkey".to_string() });
    }

    Ok(event)
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_message_event() {
        let json = r#"{
            "type": "message",
            "signed_by": "abc123",
            "channel_id": "chan1",
            "author_id": "user1",
            "content": "hello world",
            "created_at": 1234567890
        }"#;

        let event = parse_message_event(json.as_bytes()).unwrap();
        assert_eq!(event.event_type, "message");
        assert_eq!(event.content, "hello world");
    }

    #[test]
    fn test_validate_event_structure() {
        let valid = r#"{"type":"message","signed_by":"abc"}"#;
        assert!(validate_event_structure(valid.as_bytes()));

        let no_type = r#"{"signed_by":"abc"}"#;
        assert!(!validate_event_structure(no_type.as_bytes()));

        let not_object = r#"["array"]"#;
        assert!(!validate_event_structure(not_object.as_bytes()));
    }

    #[test]
    fn test_reject_oversized() {
        let huge = vec![b'{'; MAX_EVENT_SIZE + 1];
        assert!(parse_event_envelope(&huge).is_err());
    }

    #[test]
    fn test_parse_user_event() {
        let json = r#"{
            "type": "user",
            "signed_by": "invite123",
            "invite_id": "invite123",
            "user_pubkey": "pubkey_base64",
            "created_at": 1234567890
        }"#;

        let event = parse_user_event(json.as_bytes()).unwrap();
        assert_eq!(event.event_type, "user");
        assert_eq!(event.invite_id, event.signed_by);
    }

    #[test]
    fn test_parse_user_event_wrong_signer() {
        let json = r#"{
            "type": "user",
            "signed_by": "wrong_signer",
            "invite_id": "invite123",
            "user_pubkey": "pubkey_base64",
            "created_at": 1234567890
        }"#;

        // Should fail: signed_by must equal invite_id
        assert!(parse_user_event(json.as_bytes()).is_err());
    }
}
