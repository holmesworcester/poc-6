"""Fixed-size wire format encoder/decoder utilities.

This module provides shared infrastructure for wire format encoding/decoding:
- Constants for sizes, flags, signer types, and event types
- WireHeader dataclass for envelope headers
- build_envelope() and parse_envelope() for envelope handling
- get_wire_type_code() for type detection
- Helper functions (_require_len, _pad_payload, _encode_ip16, _decode_ip16)

Event-specific encode/decode functions are in their respective event modules:
- events/content/message.py for message wire functions
- events/identity/user.py for user wire functions
- etc.

See events/registry.py for wire format discovery and dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import struct
from typing import Any

from core import crypto


# ============================================================================
# Size constants
# ============================================================================

WIRE_SIZE = 512
HEADER_SIZE = 48
PAYLOAD_SIZE = 400
SIGNATURE_SIZE = 64
SIGNER_ID_SIZE = 16
RESERVED_SIZE = 8

PUBKEY_SIZE = 32
PRIVKEY_SIZE = 32
SECRET_SIZE = 32
IP_SIZE = 16
FILE_SLICE_CIPHERTEXT_SIZE = 450
FILE_SLICE_NONCE_SIZE = 24
FILE_SLICE_TAG_SIZE = 16

# Plaintext sizes (used by event modules)
MESSAGE_PLAINTEXT_SIZE = 344
CONTENT_MAX = 256
CHANNEL_PLAINTEXT_SIZE = 344
NAME_MAX = 64
FILENAME_MAX = 128
MIME_MAX = 32
MESSAGE_UPDATE_PLAINTEXT_SIZE = 344
UPDATE_MAX = 256
MESSAGE_DELETION_PLAINTEXT_SIZE = 344
MESSAGE_REACTION_PLAINTEXT_SIZE = 344
MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE = 344
MESSAGE_ATTACHMENT_PLAINTEXT_SIZE = 344
NONCE_PREFIX_SIZE = 20
MESSAGE_REKEY_PLAINTEXT_SIZE = 400
MESSAGE_REKEY_CIPHERTEXT_MAX = MESSAGE_REKEY_PLAINTEXT_SIZE - 34
CHANNEL_UPDATE_PLAINTEXT_SIZE = 344
GROUP_PLAINTEXT_SIZE = 344
GROUP_MEMBER_PLAINTEXT_SIZE = 344
GROUP_KEY_PLAINTEXT_SIZE = 344
GROUP_KEY_SHARED_PLAINTEXT_SIZE = 312
GROUP_PREKEY_PLAINTEXT_SIZE = 344
GROUP_PREKEY_SHARED_PLAINTEXT_SIZE = 344
CONNECTION_PREKEY_PLAINTEXT_SIZE = 344
CONNECTION_PREKEY_SHARED_PLAINTEXT_SIZE = 344
CONNECTION_REQUEST_PLAINTEXT_SIZE = 344
CONNECTION_ACK_PLAINTEXT_SIZE = 344
USER_PLAINTEXT_SIZE = 344
USERNAME_UPDATE_PLAINTEXT_SIZE = 344
USER_REMOVED_PLAINTEXT_SIZE = 344
PEER_PLAINTEXT_SIZE = 344
PEER_SHARED_PLAINTEXT_SIZE = 344
PEER_NAME_UPDATE_PLAINTEXT_SIZE = 344
PEER_REMOVED_PLAINTEXT_SIZE = 344
NETWORK_PLAINTEXT_SIZE = 344
NETWORK_NAME_UPDATE_PLAINTEXT_SIZE = 344
ADMIN_PLAINTEXT_SIZE = 344
INVITE_PLAINTEXT_SIZE = 344
INVITE_ACCEPTED_PLAINTEXT_SIZE = 344
SELF_ADDRESS_PLAINTEXT_SIZE = 344
OBSERVED_ADDRESS_PLAINTEXT_SIZE = 344
NETWORK_INTRO_PLAINTEXT_SIZE = 344
NEGENTROPY_PLAINTEXT_SIZE = 344


# ============================================================================
# Flags
# ============================================================================

FLAG_ENCRYPTED = 1 << 0
FLAG_WRAP_ASYM = 1 << 1
FLAG_UNSIGNED = 1 << 2


# ============================================================================
# Signer types
# ============================================================================

SIGNER_NONE = 0
SIGNER_PEER_SHARED = 1
SIGNER_USER = 2
SIGNER_INVITE = 3
SIGNER_NETWORK = 4
SIGNER_PEER = 5


def signer_type_to_str(signer_type: int) -> str:
    """Convert signer type integer to string."""
    mapping = {
        SIGNER_NONE: "none",
        SIGNER_PEER_SHARED: "peer_shared",
        SIGNER_USER: "user",
        SIGNER_INVITE: "invite",
        SIGNER_NETWORK: "network",
        SIGNER_PEER: "peer",
    }
    if signer_type not in mapping:
        raise ValueError(f"unknown signer_type id: {signer_type}")
    return mapping[signer_type]


def signer_type_from_str(signer_type: str) -> int:
    """Convert signer type string to integer."""
    mapping = {
        "none": SIGNER_NONE,
        "peer_shared": SIGNER_PEER_SHARED,
        "user": SIGNER_USER,
        "invite": SIGNER_INVITE,
        "network": SIGNER_NETWORK,
        "peer": SIGNER_PEER,
    }
    if signer_type not in mapping:
        raise ValueError(f"unknown signer_type: {signer_type}")
    return mapping[signer_type]


# ============================================================================
# Event types (wire type codes)
# ============================================================================

TYPE_MESSAGE = 0x01
TYPE_CHANNEL = 0x02
TYPE_MESSAGE_UPDATE = 0x03
TYPE_MESSAGE_DELETION = 0x04
TYPE_MESSAGE_REACTION = 0x05
TYPE_MESSAGE_REACTION_DELETION = 0x06
TYPE_MESSAGE_ATTACHMENT = 0x07
TYPE_FILE_SLICE = 0x08
TYPE_MESSAGE_REKEY = 0x09
TYPE_CHANNEL_UPDATE = 0x0A
TYPE_GROUP = 0x10
TYPE_GROUP_MEMBER = 0x11
TYPE_GROUP_KEY = 0x12
TYPE_GROUP_KEY_SHARED = 0x13
TYPE_GROUP_PREKEY = 0x14
TYPE_GROUP_PREKEY_SHARED = 0x15
TYPE_USER = 0x20
TYPE_USERNAME_UPDATE = 0x21
TYPE_USER_REMOVED = 0x22
TYPE_PEER = 0x23
TYPE_PEER_SHARED = 0x24
TYPE_PEER_NAME_UPDATE = 0x25
TYPE_PEER_REMOVED = 0x26
TYPE_NETWORK = 0x27
TYPE_NETWORK_NAME_UPDATE = 0x28
TYPE_ADMIN = 0x29
TYPE_INVITE = 0x2A
TYPE_INVITE_ACCEPTED = 0x2B
TYPE_CONNECTION_PREKEY = 0x30
TYPE_CONNECTION_PREKEY_SHARED = 0x31
TYPE_CONNECTION_REQUEST = 0x32
TYPE_CONNECTION_ACK = 0x33
TYPE_SELF_ADDRESS = 0x34
TYPE_OBSERVED_ADDRESS = 0x35
TYPE_NETWORK_INTRO = 0x36
TYPE_NEGENTROPY = 0x37

# Invite modes
INVITE_MODE_USER = 0
INVITE_MODE_PEER = 1

# Negentropy constants (kept for backwards compatibility, also in negentropy module)
NEGENTROPY_MSG_RANGE_REQUEST = 1
NEGENTROPY_MSG_RANGE_MATCHED = 2
NEGENTROPY_MSG_RANGE_EVENTS = 3

NEGENTROPY_LEVEL_ROOT = 0
NEGENTROPY_LEVEL_PREFIX_2 = 1
NEGENTROPY_LEVEL_PREFIX_4 = 2
NEGENTROPY_LEVEL_PREFIX_6 = 3

NEGENTROPY_RANGE_ID_SIZE = 8
NEGENTROPY_PREFIX_BYTES = 3
NEGENTROPY_EVENT_ID_MAX = 15


# ============================================================================
# Helper functions
# ============================================================================

_HEADER_STRUCT = struct.Struct("<BBBBIQQ16s8s")


def _require_len(name: str, value: bytes, expected: int) -> bytes:
    """Validate that value is exactly expected bytes."""
    if len(value) != expected:
        raise ValueError(f"{name} must be {expected} bytes, got {len(value)}")
    return value


def _pad_payload(plaintext: bytes) -> bytes:
    """Pad plaintext to PAYLOAD_SIZE with zeros."""
    if len(plaintext) > PAYLOAD_SIZE:
        raise ValueError(f"plaintext exceeds payload size ({len(plaintext)} > {PAYLOAD_SIZE})")
    return plaintext + (b"\x00" * (PAYLOAD_SIZE - len(plaintext)))


def _encode_ip16(ip: str | None) -> bytes:
    """Encode IP address as 16 bytes (IPv4-mapped IPv6)."""
    if not ip:
        return b"\x00" * IP_SIZE
    addr = ipaddress.ip_address(ip)
    if addr.version == 4:
        addr = ipaddress.IPv6Address(f"::ffff:{ip}")
    return _require_len("ip", addr.packed, IP_SIZE)


def _decode_ip16(data: bytes) -> str | None:
    """Decode 16-byte IP address (IPv4-mapped IPv6)."""
    _require_len("ip", data, IP_SIZE)
    if data == b"\x00" * IP_SIZE:
        return None
    addr = ipaddress.IPv6Address(data)
    if addr.ipv4_mapped:
        return str(addr.ipv4_mapped)
    return str(addr)


def _signing_bytes(header: "WireHeader", plaintext: bytes) -> bytes:
    """Build the bytes that are signed for a wire event.

    Args:
        header: Wire envelope header
        plaintext: Plaintext payload (will be padded to PAYLOAD_SIZE)

    Returns:
        Header + padded payload bytes for signing
    """
    if len(plaintext) > PAYLOAD_SIZE:
        raise ValueError("plaintext exceeds payload size")
    padded = plaintext + (b"\x00" * (PAYLOAD_SIZE - len(plaintext)))
    return header.pack() + padded


# ============================================================================
# Wire envelope header
# ============================================================================

@dataclass(frozen=True)
class WireHeader:
    """Fixed-size wire envelope header (48 bytes).

    Layout:
    - version (1 byte) - always 1
    - event_type (1 byte) - TYPE_* constant
    - flags (1 byte) - FLAG_* flags
    - signer_type (1 byte) - SIGNER_* constant
    - count (4 bytes) - event-specific count
    - created_at_ms (8 bytes) - timestamp
    - ttl_ms (8 bytes) - time-to-live
    - signer_id (16 bytes) - signer identifier
    - reserved (8 bytes) - must be zero
    """
    version: int
    event_type: int
    flags: int
    signer_type: int
    count: int
    created_at_ms: int
    ttl_ms: int
    signer_id: bytes

    def pack(self) -> bytes:
        _require_len("signer_id", self.signer_id, SIGNER_ID_SIZE)
        reserved = b"\x00" * RESERVED_SIZE
        return _HEADER_STRUCT.pack(
            self.version & 0xFF,
            self.event_type & 0xFF,
            self.flags & 0xFF,
            self.signer_type & 0xFF,
            self.count & 0xFFFFFFFF,
            self.created_at_ms & 0xFFFFFFFFFFFFFFFF,
            self.ttl_ms & 0xFFFFFFFFFFFFFFFF,
            self.signer_id,
            reserved,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "WireHeader":
        if len(data) != HEADER_SIZE:
            raise ValueError(f"header must be {HEADER_SIZE} bytes, got {len(data)}")
        version, event_type, flags, signer_type, count, created_at_ms, ttl_ms, signer_id, reserved = (
            _HEADER_STRUCT.unpack(data)
        )
        if reserved != b"\x00" * RESERVED_SIZE:
            raise ValueError("header reserved bytes must be zero")
        return cls(
            version=version,
            event_type=event_type,
            flags=flags,
            signer_type=signer_type,
            count=count,
            created_at_ms=created_at_ms,
            ttl_ms=ttl_ms,
            signer_id=signer_id,
        )


# ============================================================================
# Envelope building and parsing
# ============================================================================

def build_envelope(header: WireHeader, payload: bytes, signature: bytes) -> bytes:
    """Build a complete wire envelope from header, payload, and signature."""
    _require_len("payload", payload, PAYLOAD_SIZE)
    _require_len("signature", signature, SIGNATURE_SIZE)
    return header.pack() + payload + signature


def parse_envelope(data: bytes) -> tuple[WireHeader, bytes, bytes]:
    """Parse a wire envelope into header, payload, and signature."""
    if len(data) != WIRE_SIZE:
        raise ValueError(f"envelope must be {WIRE_SIZE} bytes, got {len(data)}")
    header = WireHeader.unpack(data[:HEADER_SIZE])
    payload = data[HEADER_SIZE:HEADER_SIZE + PAYLOAD_SIZE]
    signature = data[HEADER_SIZE + PAYLOAD_SIZE:]
    return header, payload, signature


def get_wire_type_code(data: bytes) -> int | None:
    """Extract type code from wire envelope header.

    Args:
        data: Wire envelope bytes

    Returns:
        Type code (e.g., 0x01 for message), or None if not a valid envelope
    """
    if len(data) != WIRE_SIZE:
        return None
    # Check version byte first
    if data[0] != 1:
        return None
    # Try standard header parse
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
        return header.event_type
    except ValueError:
        # Some event types (like file_slice) have non-standard headers
        # but still use version=1 and type at byte 1
        return data[1]
