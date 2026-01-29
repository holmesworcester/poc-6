"""Fixed-size wire format encoder/decoder utilities.

This module implements the common envelope and a pilot codec for the
`message` event payload. It is intentionally strict to support LangSec-style
parsers and avoid ambiguous decoding.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import struct
from typing import Any

from core import crypto


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

# TreeKEM Phase 1 plaintext sizes
PUBKEY_PLAINTEXT_SIZE = 344
SECRET_PLAINTEXT_SIZE = 344
SECRET_SHARED_PLAINTEXT_SIZE = 344
REMOVAL_EPOCH_PLAINTEXT_SIZE = 344
KEY_REQUEST_PLAINTEXT_SIZE = 344

# TreeKEM Phase 2 plaintext sizes
TREEKEM_SECRET_PLAINTEXT_SIZE = 344
TREEKEM_PUBKEY_PLAINTEXT_SIZE = 344
TREEKEM_UPDATE_PLAINTEXT_SIZE = 344
TREEKEM_SECRET_SHARED_PLAINTEXT_SIZE = 344

# Flags
FLAG_ENCRYPTED = 1 << 0
FLAG_WRAP_ASYM = 1 << 1
FLAG_UNSIGNED = 1 << 2

# Signer types
SIGNER_NONE = 0
SIGNER_PEER_SHARED = 1
SIGNER_USER = 2
SIGNER_INVITE = 3
SIGNER_NETWORK = 4
SIGNER_PEER = 5

# Event types
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

# TreeKEM Phase 1 event types
TYPE_PUBKEY = 0x40
TYPE_SECRET = 0x41
TYPE_SECRET_SHARED = 0x42
TYPE_REMOVAL_EPOCH = 0x43
TYPE_KEY_REQUEST = 0x44

# TreeKEM Phase 2 event types
TYPE_TREEKEM_SECRET = 0x45
TYPE_TREEKEM_PUBKEY = 0x46
TYPE_TREEKEM_UPDATE = 0x47
TYPE_TREEKEM_SECRET_SHARED = 0x48

INVITE_MODE_USER = 0
INVITE_MODE_PEER = 1

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

_HEADER_STRUCT = struct.Struct("<BBBBIQQ16s8s")


def _require_len(name: str, value: bytes, expected: int) -> bytes:
    if len(value) != expected:
        raise ValueError(f"{name} must be {expected} bytes, got {len(value)}")
    return value


def _pad_payload(plaintext: bytes) -> bytes:
    if len(plaintext) > PAYLOAD_SIZE:
        raise ValueError(f"plaintext exceeds payload size ({len(plaintext)} > {PAYLOAD_SIZE})")
    return plaintext + (b"\x00" * (PAYLOAD_SIZE - len(plaintext)))


def _encode_ip16(ip: str | None) -> bytes:
    if not ip:
        return b"\x00" * IP_SIZE
    addr = ipaddress.ip_address(ip)
    if addr.version == 4:
        addr = ipaddress.IPv6Address(f"::ffff:{ip}")
    return _require_len("ip", addr.packed, IP_SIZE)


def _decode_ip16(data: bytes) -> str | None:
    _require_len("ip", data, IP_SIZE)
    if data == b"\x00" * IP_SIZE:
        return None
    addr = ipaddress.IPv6Address(data)
    if addr.ipv4_mapped:
        return str(addr.ipv4_mapped)
    return str(addr)


@dataclass(frozen=True)
class WireHeader:
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


def build_envelope(header: WireHeader, payload: bytes, signature: bytes) -> bytes:
    _require_len("payload", payload, PAYLOAD_SIZE)
    _require_len("signature", signature, SIGNATURE_SIZE)
    return header.pack() + payload + signature


def parse_envelope(data: bytes) -> tuple[WireHeader, bytes, bytes]:
    if len(data) != WIRE_SIZE:
        raise ValueError(f"envelope must be {WIRE_SIZE} bytes, got {len(data)}")
    header = WireHeader.unpack(data[:HEADER_SIZE])
    payload = data[HEADER_SIZE:HEADER_SIZE + PAYLOAD_SIZE]
    signature = data[HEADER_SIZE + PAYLOAD_SIZE:]
    return header, payload, signature


def encode_message_plaintext(
    channel_id: bytes,
    author_id: bytes,
    content: str | bytes,
    disappearing_time_ms: int,
) -> bytes:
    """Encode a message payload plaintext (pre-encryption).

    Layout (344 bytes):
    - channel_id (16)
    - author_id (16)
    - disappearing_time_ms (u64)
    - content_len (u16)
    - content_bytes (CONTENT_MAX)
    - pad
    """
    _require_len("channel_id", channel_id, 16)
    _require_len("author_id", author_id, 16)

    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = bytes(content)

    if len(content_bytes) > CONTENT_MAX:
        raise ValueError(f"content exceeds {CONTENT_MAX} bytes, got {len(content_bytes)}")

    payload = bytearray(MESSAGE_PLAINTEXT_SIZE)
    payload[0:16] = channel_id
    payload[16:32] = author_id
    struct.pack_into("<Q", payload, 32, disappearing_time_ms)
    struct.pack_into("<H", payload, 40, len(content_bytes))
    payload[42:42 + len(content_bytes)] = content_bytes
    return bytes(payload)


def decode_message_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message payload plaintext (post-decryption)."""
    if len(data) != MESSAGE_PLAINTEXT_SIZE:
        raise ValueError(f"message plaintext must be {MESSAGE_PLAINTEXT_SIZE} bytes, got {len(data)}")

    channel_id = data[0:16]
    author_id = data[16:32]
    (disappearing_time_ms,) = struct.unpack_from("<Q", data, 32)
    (content_len,) = struct.unpack_from("<H", data, 40)

    if content_len > CONTENT_MAX:
        raise ValueError(f"content_len exceeds {CONTENT_MAX}, got {content_len}")

    content_bytes = data[42:42 + content_len]
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("content is not valid utf-8") from exc

    return {
        "channel_id": channel_id,
        "author_id": author_id,
        "disappearing_time_ms": disappearing_time_ms,
        "content": content,
    }


def encode_message_update_plaintext(
    message_id: bytes,
    group_id: bytes,
    edited_by: bytes,
    author_id: bytes,
    new_content: str | bytes,
) -> bytes:
    """Encode a message_update payload plaintext (pre-encryption).

    Layout (344 bytes):
    - message_id (16)
    - group_id (16)
    - edited_by (16)
    - author_id (16)
    - new_content_len (u16)
    - new_content_bytes (UPDATE_MAX)
    - pad
    """
    _require_len("message_id", message_id, 16)
    _require_len("group_id", group_id, 16)
    _require_len("edited_by", edited_by, 16)
    _require_len("author_id", author_id, 16)

    if isinstance(new_content, str):
        content_bytes = new_content.encode("utf-8")
    else:
        content_bytes = bytes(new_content)

    if len(content_bytes) > UPDATE_MAX:
        raise ValueError(f"new_content exceeds {UPDATE_MAX} bytes, got {len(content_bytes)}")

    payload = bytearray(MESSAGE_UPDATE_PLAINTEXT_SIZE)
    payload[0:16] = message_id
    payload[16:32] = group_id
    payload[32:48] = edited_by
    payload[48:64] = author_id
    struct.pack_into("<H", payload, 64, len(content_bytes))
    payload[66:66 + len(content_bytes)] = content_bytes
    return bytes(payload)


def decode_message_update_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_update payload plaintext (post-decryption)."""
    if len(data) != MESSAGE_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_update plaintext must be {MESSAGE_UPDATE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )

    message_id = data[0:16]
    group_id = data[16:32]
    edited_by = data[32:48]
    author_id = data[48:64]
    (content_len,) = struct.unpack_from("<H", data, 64)

    if content_len > UPDATE_MAX:
        raise ValueError(f"new_content_len exceeds {UPDATE_MAX}, got {content_len}")

    content_bytes = data[66:66 + content_len]
    try:
        new_content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("new_content is not valid utf-8") from exc

    return {
        "message_id": message_id,
        "group_id": group_id,
        "edited_by": edited_by,
        "author_id": author_id,
        "new_content": new_content,
    }


def encode_message_deletion_plaintext(message_id: bytes) -> bytes:
    """Encode a message_deletion payload plaintext (pre-encryption)."""
    _require_len("message_id", message_id, 16)
    payload = bytearray(MESSAGE_DELETION_PLAINTEXT_SIZE)
    payload[0:16] = message_id
    return bytes(payload)


def decode_message_deletion_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_deletion payload plaintext (post-decryption)."""
    if len(data) != MESSAGE_DELETION_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_deletion plaintext must be {MESSAGE_DELETION_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"message_id": data[0:16]}


def encode_message_reaction_plaintext(
    message_id: bytes,
    reactor_id: bytes,
    emoji: str,
) -> bytes:
    """Encode a message_reaction payload plaintext (pre-encryption)."""
    _require_len("message_id", message_id, 16)
    _require_len("reactor_id", reactor_id, 16)
    if not isinstance(emoji, str):
        raise ValueError("emoji must be a single unicode codepoint")
    if len(emoji) != 1:
        # Allow variation selectors (e.g., "❤️" -> "❤")
        stripped = "".join(ch for ch in emoji if ord(ch) not in (0xFE0E, 0xFE0F))
        if len(stripped) != 1:
            raise ValueError("emoji must be a single unicode codepoint")
        emoji = stripped
    codepoint = ord(emoji)
    if codepoint > 0x10FFFF:
        raise ValueError("emoji codepoint out of range")
    payload = bytearray(MESSAGE_REACTION_PLAINTEXT_SIZE)
    payload[0:16] = message_id
    payload[16:32] = reactor_id
    struct.pack_into("<I", payload, 32, codepoint)
    return bytes(payload)


def decode_message_reaction_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_reaction payload plaintext (post-decryption)."""
    if len(data) != MESSAGE_REACTION_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_reaction plaintext must be {MESSAGE_REACTION_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    message_id = data[0:16]
    reactor_id = data[16:32]
    (codepoint,) = struct.unpack_from("<I", data, 32)
    emoji = chr(codepoint) if codepoint else ""
    if emoji == "\u2764":
        emoji = "\u2764\uFE0F"
    return {
        "message_id": message_id,
        "reactor_id": reactor_id,
        "emoji": emoji,
    }


def encode_message_reaction_deletion_plaintext(reaction_id: bytes) -> bytes:
    """Encode a message_reaction_deletion payload plaintext (pre-encryption)."""
    _require_len("reaction_id", reaction_id, 16)
    payload = bytearray(MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE)
    payload[0:16] = reaction_id
    return bytes(payload)


def decode_message_reaction_deletion_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_reaction_deletion payload plaintext (post-decryption)."""
    if len(data) != MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE:
        raise ValueError(
            "message_reaction_deletion plaintext must be "
            f"{MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"reaction_id": data[0:16]}


def encode_message_attachment_plaintext(
    message_id: bytes,
    file_id: bytes,
    blob_bytes: int,
    total_slices: int,
    nonce_prefix: bytes,
    enc_key: bytes,
    root_hash: bytes,
    filename: str | bytes | None,
    mime_type: str | bytes | None,
) -> bytes:
    """Encode a message_attachment payload plaintext (pre-encryption)."""
    _require_len("message_id", message_id, 16)
    _require_len("file_id", file_id, 16)
    _require_len("nonce_prefix", nonce_prefix, NONCE_PREFIX_SIZE)
    _require_len("enc_key", enc_key, SECRET_SIZE)
    _require_len("root_hash", root_hash, 32)
    if blob_bytes < 0:
        raise ValueError("blob_bytes must be non-negative")
    if total_slices < 0 or total_slices > 0xFFFFFFFF:
        raise ValueError("total_slices must fit in u32")

    if filename is None:
        filename_bytes = b""
    elif isinstance(filename, str):
        filename_bytes = filename.encode("utf-8")
    else:
        filename_bytes = bytes(filename)

    if mime_type is None:
        mime_bytes = b""
    elif isinstance(mime_type, str):
        mime_bytes = mime_type.encode("utf-8")
    else:
        mime_bytes = bytes(mime_type)

    if len(filename_bytes) > FILENAME_MAX:
        raise ValueError(f"filename exceeds {FILENAME_MAX} bytes, got {len(filename_bytes)}")
    if len(mime_bytes) > MIME_MAX:
        raise ValueError(f"mime_type exceeds {MIME_MAX} bytes, got {len(mime_bytes)}")

    payload = bytearray(MESSAGE_ATTACHMENT_PLAINTEXT_SIZE)
    payload[0:16] = message_id
    payload[16:32] = file_id
    struct.pack_into("<Q", payload, 32, blob_bytes)
    struct.pack_into("<I", payload, 40, total_slices)
    payload[44:44 + NONCE_PREFIX_SIZE] = nonce_prefix
    payload[64:96] = enc_key
    payload[96:128] = root_hash
    struct.pack_into("<H", payload, 128, len(filename_bytes))
    payload[130:130 + len(filename_bytes)] = filename_bytes
    mime_len_offset = 130 + FILENAME_MAX
    struct.pack_into("<H", payload, mime_len_offset, len(mime_bytes))
    payload[mime_len_offset + 2:mime_len_offset + 2 + len(mime_bytes)] = mime_bytes
    return bytes(payload)


def decode_message_attachment_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_attachment payload plaintext (post-decryption)."""
    if len(data) != MESSAGE_ATTACHMENT_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_attachment plaintext must be {MESSAGE_ATTACHMENT_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    message_id = data[0:16]
    file_id = data[16:32]
    (blob_bytes,) = struct.unpack_from("<Q", data, 32)
    (total_slices,) = struct.unpack_from("<I", data, 40)
    nonce_prefix = data[44:44 + NONCE_PREFIX_SIZE]
    enc_key = data[64:96]
    root_hash = data[96:128]
    (filename_len,) = struct.unpack_from("<H", data, 128)
    if filename_len > FILENAME_MAX:
        raise ValueError(f"filename_len exceeds {FILENAME_MAX}, got {filename_len}")
    filename_bytes = data[130:130 + filename_len]
    (mime_len,) = struct.unpack_from("<H", data, 130 + FILENAME_MAX)
    if mime_len > MIME_MAX:
        raise ValueError(f"mime_len exceeds {MIME_MAX}, got {mime_len}")
    mime_bytes = data[132 + FILENAME_MAX:132 + FILENAME_MAX + mime_len]

    if filename_len:
        try:
            filename = filename_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("filename is not valid utf-8") from exc
    else:
        filename = None
    if mime_len:
        try:
            mime_type = mime_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("mime_type is not valid utf-8") from exc
    else:
        mime_type = None
    return {
        "message_id": message_id,
        "file_id": file_id,
        "blob_bytes": blob_bytes,
        "total_slices": total_slices,
        "nonce_prefix": nonce_prefix,
        "enc_key": enc_key,
        "root_hash": root_hash,
        "filename": filename,
        "mime_type": mime_type,
    }


def encode_message_rekey_plaintext(
    original_message_id: bytes,
    new_key_id: bytes,
    new_ciphertext: bytes,
) -> bytes:
    """Encode a message_rekey payload plaintext (pre-encryption)."""
    _require_len("original_message_id", original_message_id, 16)
    _require_len("new_key_id", new_key_id, 16)
    new_ciphertext = bytes(new_ciphertext)
    if len(new_ciphertext) > MESSAGE_REKEY_CIPHERTEXT_MAX:
        raise ValueError(
            f"new_ciphertext exceeds {MESSAGE_REKEY_CIPHERTEXT_MAX} bytes, got {len(new_ciphertext)}"
        )
    payload = bytearray(MESSAGE_REKEY_PLAINTEXT_SIZE)
    payload[0:16] = original_message_id
    payload[16:32] = new_key_id
    struct.pack_into("<H", payload, 32, len(new_ciphertext))
    payload[34:34 + len(new_ciphertext)] = new_ciphertext
    return bytes(payload)


def decode_message_rekey_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_rekey payload plaintext (post-decryption)."""
    if len(data) != MESSAGE_REKEY_PLAINTEXT_SIZE:
        raise ValueError(f"message_rekey plaintext must be {MESSAGE_REKEY_PLAINTEXT_SIZE} bytes, got {len(data)}")
    original_message_id = data[0:16]
    new_key_id = data[16:32]
    (ciphertext_len,) = struct.unpack_from("<H", data, 32)
    if ciphertext_len > MESSAGE_REKEY_CIPHERTEXT_MAX:
        raise ValueError(f"new_ciphertext_len exceeds {MESSAGE_REKEY_CIPHERTEXT_MAX}, got {ciphertext_len}")
    new_ciphertext = data[34:34 + ciphertext_len]
    return {
        "original_message_id": original_message_id,
        "new_key_id": new_key_id,
        "new_ciphertext": new_ciphertext,
    }


def encode_channel_plaintext(
    group_id: bytes,
    name: str | bytes,
    disappearing_time_ms: int,
    is_main: int | bool,
    admin_grant_id: bytes | None,
) -> bytes:
    """Encode a channel payload plaintext (pre-encryption).

    Layout (344 bytes):
    - group_id (16)
    - name_len (u16)
    - name_bytes (NAME_MAX)
    - disappearing_time_ms (u64)
    - is_main (u8)
    - admin_grant_id (16, zero if none)
    - pad
    """
    _require_len("group_id", group_id, 16)

    if isinstance(name, str):
        name_bytes = name.encode("utf-8")
    else:
        name_bytes = bytes(name)

    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")

    admin_grant_bytes = admin_grant_id or (b"\x00" * 16)
    _require_len("admin_grant_id", admin_grant_bytes, 16)

    payload = bytearray(CHANNEL_PLAINTEXT_SIZE)
    payload[0:16] = group_id
    struct.pack_into("<H", payload, 16, len(name_bytes))
    payload[18:18 + len(name_bytes)] = name_bytes
    struct.pack_into("<Q", payload, 18 + NAME_MAX, disappearing_time_ms)
    payload[26 + NAME_MAX] = 1 if is_main else 0
    payload[27 + NAME_MAX:27 + NAME_MAX + 16] = admin_grant_bytes
    return bytes(payload)


def decode_channel_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a channel payload plaintext (post-decryption)."""
    if len(data) != CHANNEL_PLAINTEXT_SIZE:
        raise ValueError(f"channel plaintext must be {CHANNEL_PLAINTEXT_SIZE} bytes, got {len(data)}")

    group_id = data[0:16]
    (name_len,) = struct.unpack_from("<H", data, 16)
    if name_len > NAME_MAX:
        raise ValueError(f"name_len exceeds {NAME_MAX}, got {name_len}")

    name_bytes = data[18:18 + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("name is not valid utf-8") from exc

    (disappearing_time_ms,) = struct.unpack_from("<Q", data, 18 + NAME_MAX)
    is_main = data[26 + NAME_MAX]
    admin_grant_id = data[27 + NAME_MAX:27 + NAME_MAX + 16]
    if admin_grant_id == b"\x00" * 16:
        admin_grant_id = None

    return {
        "group_id": group_id,
        "name": name,
        "disappearing_time_ms": disappearing_time_ms,
        "is_main": is_main,
        "admin_grant_id": admin_grant_id,
    }


def encode_channel_update_plaintext(
    channel_id: bytes,
    group_id: bytes,
    updated_by: bytes,
    new_channel_name: str | bytes | None,
    new_disappearing_time_ms: int | None,
) -> bytes:
    """Encode a channel_update payload plaintext (pre-encryption)."""
    _require_len("channel_id", channel_id, 16)
    _require_len("group_id", group_id, 16)
    _require_len("updated_by", updated_by, 16)
    if new_channel_name is None:
        name_bytes = b""
    elif isinstance(new_channel_name, str):
        name_bytes = new_channel_name.encode("utf-8")
    else:
        name_bytes = bytes(new_channel_name)
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"new_channel_name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")
    ttl_value = 0xFFFFFFFFFFFFFFFF if new_disappearing_time_ms is None else new_disappearing_time_ms
    if ttl_value < 0 or ttl_value > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("new_disappearing_time_ms must fit in u64")

    payload = bytearray(CHANNEL_UPDATE_PLAINTEXT_SIZE)
    payload[0:16] = channel_id
    payload[16:32] = group_id
    payload[32:48] = updated_by
    struct.pack_into("<H", payload, 48, len(name_bytes))
    payload[50:50 + len(name_bytes)] = name_bytes
    struct.pack_into("<Q", payload, 50 + NAME_MAX, ttl_value)
    return bytes(payload)


def decode_channel_update_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a channel_update payload plaintext (post-decryption)."""
    if len(data) != CHANNEL_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(
            f"channel_update plaintext must be {CHANNEL_UPDATE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    channel_id = data[0:16]
    group_id = data[16:32]
    updated_by = data[32:48]
    (name_len,) = struct.unpack_from("<H", data, 48)
    if name_len > NAME_MAX:
        raise ValueError(f"new_channel_name_len exceeds {NAME_MAX}, got {name_len}")
    name_bytes = data[50:50 + name_len]
    new_channel_name = name_bytes.decode("utf-8") if name_len else None
    (new_disappearing_time_ms,) = struct.unpack_from("<Q", data, 50 + NAME_MAX)
    if new_disappearing_time_ms == 0xFFFFFFFFFFFFFFFF:
        new_disappearing_time_ms = None
    return {
        "channel_id": channel_id,
        "group_id": group_id,
        "updated_by": updated_by,
        "new_channel_name": new_channel_name,
        "new_disappearing_time_ms": new_disappearing_time_ms,
    }


def encode_group_plaintext(
    name: str | bytes,
    key_id: bytes,
    is_main: int | bool,
    network_id: bytes | None,
) -> bytes:
    """Encode a group payload plaintext (pre-encryption)."""
    _require_len("key_id", key_id, 16)
    if isinstance(name, str):
        name_bytes = name.encode("utf-8")
    else:
        name_bytes = bytes(name)
    if not name_bytes:
        raise ValueError("group name must not be empty")
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")
    network_bytes = network_id or (b"\x00" * 16)
    _require_len("network_id", network_bytes, 16)

    payload = bytearray(GROUP_PLAINTEXT_SIZE)
    struct.pack_into("<H", payload, 0, len(name_bytes))
    payload[2:2 + len(name_bytes)] = name_bytes
    payload[2 + NAME_MAX:2 + NAME_MAX + 16] = key_id
    payload[18 + NAME_MAX] = 1 if is_main else 0
    payload[19 + NAME_MAX:19 + NAME_MAX + 16] = network_bytes
    return bytes(payload)


def decode_group_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group payload plaintext (post-decryption)."""
    if len(data) != GROUP_PLAINTEXT_SIZE:
        raise ValueError(f"group plaintext must be {GROUP_PLAINTEXT_SIZE} bytes, got {len(data)}")
    (name_len,) = struct.unpack_from("<H", data, 0)
    if name_len > NAME_MAX:
        raise ValueError(f"name_len exceeds {NAME_MAX}, got {name_len}")
    name_bytes = data[2:2 + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("name is not valid utf-8") from exc
    key_id = data[2 + NAME_MAX:2 + NAME_MAX + 16]
    is_main = data[18 + NAME_MAX]
    network_id = data[19 + NAME_MAX:19 + NAME_MAX + 16]
    if network_id == b"\x00" * 16:
        network_id = None
    return {
        "name": name,
        "key_id": key_id,
        "is_main": is_main,
        "network_id": network_id,
    }


def encode_group_member_plaintext(
    group_id: bytes,
    user_id: bytes,
    added_by: bytes,
    admin_grant_id: bytes | None,
) -> bytes:
    """Encode a group_member payload plaintext (pre-encryption)."""
    _require_len("group_id", group_id, 16)
    _require_len("user_id", user_id, 16)
    _require_len("added_by", added_by, 16)
    admin_grant_bytes = admin_grant_id or (b"\x00" * 16)
    _require_len("admin_grant_id", admin_grant_bytes, 16)
    payload = bytearray(GROUP_MEMBER_PLAINTEXT_SIZE)
    payload[0:16] = group_id
    payload[16:32] = user_id
    payload[32:48] = added_by
    payload[48:64] = admin_grant_bytes
    return bytes(payload)


def decode_group_member_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_member payload plaintext (post-decryption)."""
    if len(data) != GROUP_MEMBER_PLAINTEXT_SIZE:
        raise ValueError(f"group_member plaintext must be {GROUP_MEMBER_PLAINTEXT_SIZE} bytes, got {len(data)}")
    group_id = data[0:16]
    user_id = data[16:32]
    added_by = data[32:48]
    admin_grant_id = data[48:64]
    if admin_grant_id == b"\x00" * 16:
        admin_grant_id = None
    return {
        "group_id": group_id,
        "user_id": user_id,
        "added_by": added_by,
        "admin_grant_id": admin_grant_id,
    }


def encode_group_key_plaintext(key: bytes) -> bytes:
    """Encode a group_key payload plaintext."""
    _require_len("key", key, SECRET_SIZE)
    payload = bytearray(GROUP_KEY_PLAINTEXT_SIZE)
    payload[0:SECRET_SIZE] = key
    return bytes(payload)


def decode_group_key_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_key payload plaintext."""
    if len(data) != GROUP_KEY_PLAINTEXT_SIZE:
        raise ValueError(f"group_key plaintext must be {GROUP_KEY_PLAINTEXT_SIZE} bytes, got {len(data)}")
    return {"key": data[0:SECRET_SIZE]}


def encode_group_key_shared_plaintext(
    key_id: bytes,
    symmetric_key: bytes,
    recipient_prekey_id: bytes,
) -> bytes:
    """Encode a group_key_shared payload plaintext (pre-encryption)."""
    _require_len("key_id", key_id, 16)
    _require_len("symmetric_key", symmetric_key, SECRET_SIZE)
    _require_len("recipient_prekey_id", recipient_prekey_id, 16)
    payload = bytearray(GROUP_KEY_SHARED_PLAINTEXT_SIZE)
    payload[0:16] = key_id
    payload[16:48] = symmetric_key
    payload[48:64] = recipient_prekey_id
    return bytes(payload)


def decode_group_key_shared_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_key_shared payload plaintext (post-decryption)."""
    if len(data) != GROUP_KEY_SHARED_PLAINTEXT_SIZE:
        raise ValueError(
            f"group_key_shared plaintext must be {GROUP_KEY_SHARED_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "key_id": data[0:16],
        "symmetric_key": data[16:48],
        "recipient_prekey_id": data[48:64],
    }


def encode_group_prekey_plaintext(public_key: bytes, private_key: bytes) -> bytes:
    """Encode a group_prekey payload plaintext."""
    _require_len("public_key", public_key, PUBKEY_SIZE)
    _require_len("private_key", private_key, PRIVKEY_SIZE)
    payload = bytearray(GROUP_PREKEY_PLAINTEXT_SIZE)
    payload[0:PUBKEY_SIZE] = public_key
    payload[PUBKEY_SIZE:PUBKEY_SIZE + PRIVKEY_SIZE] = private_key
    return bytes(payload)


def decode_group_prekey_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_prekey payload plaintext."""
    if len(data) != GROUP_PREKEY_PLAINTEXT_SIZE:
        raise ValueError(f"group_prekey plaintext must be {GROUP_PREKEY_PLAINTEXT_SIZE} bytes, got {len(data)}")
    return {
        "public_key": data[0:PUBKEY_SIZE],
        "private_key": data[PUBKEY_SIZE:PUBKEY_SIZE + PRIVKEY_SIZE],
    }


def encode_group_prekey_shared_plaintext(
    group_prekey_id: bytes,
    peer_id: bytes,
    public_key: bytes,
) -> bytes:
    """Encode a group_prekey_shared payload plaintext."""
    _require_len("group_prekey_id", group_prekey_id, 16)
    _require_len("peer_id", peer_id, 16)
    _require_len("public_key", public_key, PUBKEY_SIZE)
    payload = bytearray(GROUP_PREKEY_SHARED_PLAINTEXT_SIZE)
    payload[0:16] = group_prekey_id
    payload[16:32] = peer_id
    payload[32:64] = public_key
    return bytes(payload)


def decode_group_prekey_shared_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_prekey_shared payload plaintext."""
    if len(data) != GROUP_PREKEY_SHARED_PLAINTEXT_SIZE:
        raise ValueError(
            f"group_prekey_shared plaintext must be {GROUP_PREKEY_SHARED_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "group_prekey_id": data[0:16],
        "peer_id": data[16:32],
        "public_key": data[32:64],
    }


def encode_connection_prekey_plaintext(public_key: bytes, private_key: bytes) -> bytes:
    """Encode a connection_prekey payload plaintext."""
    _require_len("public_key", public_key, PUBKEY_SIZE)
    _require_len("private_key", private_key, PRIVKEY_SIZE)
    payload = bytearray(CONNECTION_PREKEY_PLAINTEXT_SIZE)
    payload[0:PUBKEY_SIZE] = public_key
    payload[PUBKEY_SIZE:PUBKEY_SIZE + PRIVKEY_SIZE] = private_key
    return bytes(payload)


def decode_connection_prekey_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a connection_prekey payload plaintext."""
    if len(data) != CONNECTION_PREKEY_PLAINTEXT_SIZE:
        raise ValueError(
            f"connection_prekey plaintext must be {CONNECTION_PREKEY_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "public_key": data[0:PUBKEY_SIZE],
        "private_key": data[PUBKEY_SIZE:PUBKEY_SIZE + PRIVKEY_SIZE],
    }


def encode_connection_prekey_shared_plaintext(
    connection_prekey_id: bytes,
    peer_id: bytes,
    public_key: bytes,
) -> bytes:
    """Encode a connection_prekey_shared payload plaintext."""
    _require_len("connection_prekey_id", connection_prekey_id, 16)
    _require_len("peer_id", peer_id, 16)
    _require_len("public_key", public_key, PUBKEY_SIZE)
    payload = bytearray(CONNECTION_PREKEY_SHARED_PLAINTEXT_SIZE)
    payload[0:16] = connection_prekey_id
    payload[16:32] = peer_id
    payload[32:64] = public_key
    return bytes(payload)


def decode_connection_prekey_shared_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a connection_prekey_shared payload plaintext."""
    if len(data) != CONNECTION_PREKEY_SHARED_PLAINTEXT_SIZE:
        raise ValueError(
            "connection_prekey_shared plaintext must be "
            f"{CONNECTION_PREKEY_SHARED_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "connection_prekey_id": data[0:16],
        "peer_id": data[16:32],
        "public_key": data[32:64],
    }


def encode_connection_request_plaintext(
    key: bytes,
    to_peer_shared_id: bytes | None,
    invite_id: bytes | None,
) -> bytes:
    """Encode a connection_request payload plaintext."""
    _require_len("key", key, SECRET_SIZE)
    to_peer_bytes = to_peer_shared_id or (b"\x00" * 16)
    invite_bytes = invite_id or (b"\x00" * 16)
    _require_len("to_peer_shared_id", to_peer_bytes, 16)
    _require_len("invite_id", invite_bytes, 16)
    payload = bytearray(CONNECTION_REQUEST_PLAINTEXT_SIZE)
    payload[0:SECRET_SIZE] = key
    payload[32:48] = to_peer_bytes
    payload[48:64] = invite_bytes
    return bytes(payload)


def decode_connection_request_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a connection_request payload plaintext."""
    if len(data) != CONNECTION_REQUEST_PLAINTEXT_SIZE:
        raise ValueError(
            f"connection_request plaintext must be {CONNECTION_REQUEST_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    key = data[0:SECRET_SIZE]
    to_peer_shared_id = data[32:48]
    invite_id = data[48:64]
    if to_peer_shared_id == b"\x00" * 16:
        to_peer_shared_id = None
    if invite_id == b"\x00" * 16:
        invite_id = None
    return {
        "key": key,
        "to_peer_shared_id": to_peer_shared_id,
        "invite_id": invite_id,
    }


def encode_connection_ack_plaintext(for_request_id: bytes, key: bytes) -> bytes:
    """Encode a connection_ack payload plaintext."""
    _require_len("for_request_id", for_request_id, 16)
    _require_len("key", key, SECRET_SIZE)
    payload = bytearray(CONNECTION_ACK_PLAINTEXT_SIZE)
    payload[0:16] = for_request_id
    payload[16:48] = key
    return bytes(payload)


def decode_connection_ack_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a connection_ack payload plaintext."""
    if len(data) != CONNECTION_ACK_PLAINTEXT_SIZE:
        raise ValueError(
            f"connection_ack plaintext must be {CONNECTION_ACK_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "for_request_id": data[0:16],
        "key": data[16:48],
    }


def encode_user_plaintext(
    invite_id: bytes,
    user_pubkey: bytes,
    network_id: bytes | None,
) -> bytes:
    """Encode a user payload plaintext (pre-encryption)."""
    _require_len("invite_id", invite_id, 16)
    _require_len("user_pubkey", user_pubkey, PUBKEY_SIZE)
    network_bytes = network_id or (b"\x00" * 16)
    _require_len("network_id", network_bytes, 16)

    payload = bytearray(USER_PLAINTEXT_SIZE)
    payload[0:16] = invite_id
    payload[16:48] = user_pubkey
    payload[48:64] = network_bytes
    return bytes(payload)


def decode_user_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a user payload plaintext (post-decryption)."""
    if len(data) != USER_PLAINTEXT_SIZE:
        raise ValueError(f"user plaintext must be {USER_PLAINTEXT_SIZE} bytes, got {len(data)}")
    invite_id = data[0:16]
    user_pubkey = data[16:48]
    network_id = data[48:64]
    if network_id == b"\x00" * 16:
        network_id = None
    return {
        "invite_id": invite_id,
        "user_pubkey": user_pubkey,
        "network_id": network_id,
    }


def encode_username_update_plaintext(user_id: bytes, name: str | bytes) -> bytes:
    """Encode a username_update payload plaintext (pre-encryption)."""
    _require_len("user_id", user_id, 16)
    if isinstance(name, str):
        name_bytes = name.encode("utf-8")
    else:
        name_bytes = bytes(name)
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")
    payload = bytearray(USERNAME_UPDATE_PLAINTEXT_SIZE)
    payload[0:16] = user_id
    struct.pack_into("<H", payload, 16, len(name_bytes))
    payload[18:18 + len(name_bytes)] = name_bytes
    return bytes(payload)


def decode_username_update_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a username_update payload plaintext (post-decryption)."""
    if len(data) != USERNAME_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(
            f"username_update plaintext must be {USERNAME_UPDATE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    user_id = data[0:16]
    (name_len,) = struct.unpack_from("<H", data, 16)
    if name_len > NAME_MAX:
        raise ValueError(f"name_len exceeds {NAME_MAX}, got {name_len}")
    name_bytes = data[18:18 + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("name is not valid utf-8") from exc
    return {"user_id": user_id, "name": name}


def encode_user_removed_plaintext(removed_user_id: bytes) -> bytes:
    """Encode a user_removed payload plaintext (pre-encryption)."""
    _require_len("removed_user_id", removed_user_id, 16)
    payload = bytearray(USER_REMOVED_PLAINTEXT_SIZE)
    payload[0:16] = removed_user_id
    return bytes(payload)


def decode_user_removed_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a user_removed payload plaintext (post-decryption)."""
    if len(data) != USER_REMOVED_PLAINTEXT_SIZE:
        raise ValueError(
            f"user_removed plaintext must be {USER_REMOVED_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"removed_user_id": data[0:16]}


def encode_peer_plaintext(public_key: bytes, private_key: bytes) -> bytes:
    """Encode a peer payload plaintext (pre-encryption)."""
    _require_len("public_key", public_key, PUBKEY_SIZE)
    _require_len("private_key", private_key, PRIVKEY_SIZE)
    payload = bytearray(PEER_PLAINTEXT_SIZE)
    payload[0:32] = public_key
    payload[32:64] = private_key
    return bytes(payload)


def decode_peer_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a peer payload plaintext (post-decryption)."""
    if len(data) != PEER_PLAINTEXT_SIZE:
        raise ValueError(f"peer plaintext must be {PEER_PLAINTEXT_SIZE} bytes, got {len(data)}")
    return {
        "public_key": data[0:32],
        "private_key": data[32:64],
    }


def encode_peer_shared_plaintext(
    public_key: bytes,
    peer_id: bytes,
    invite_id: bytes | None,
) -> bytes:
    """Encode a peer_shared payload plaintext (pre-encryption)."""
    _require_len("public_key", public_key, PUBKEY_SIZE)
    _require_len("peer_id", peer_id, 16)
    invite_bytes = invite_id or (b"\x00" * 16)
    _require_len("invite_id", invite_bytes, 16)
    payload = bytearray(PEER_SHARED_PLAINTEXT_SIZE)
    payload[0:32] = public_key
    payload[32:48] = peer_id
    payload[48:64] = invite_bytes
    return bytes(payload)


def decode_peer_shared_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a peer_shared payload plaintext (post-decryption)."""
    if len(data) != PEER_SHARED_PLAINTEXT_SIZE:
        raise ValueError(
            f"peer_shared plaintext must be {PEER_SHARED_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    public_key = data[0:32]
    peer_id = data[32:48]
    invite_id = data[48:64]
    if invite_id == b"\x00" * 16:
        invite_id = None
    return {"public_key": public_key, "peer_id": peer_id, "invite_id": invite_id}


def encode_peer_name_update_plaintext(peer_id: bytes, name: str | bytes) -> bytes:
    """Encode a peer_name_update payload plaintext (pre-encryption)."""
    _require_len("peer_id", peer_id, 16)
    if isinstance(name, str):
        name_bytes = name.encode("utf-8")
    else:
        name_bytes = bytes(name)
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")
    payload = bytearray(PEER_NAME_UPDATE_PLAINTEXT_SIZE)
    payload[0:16] = peer_id
    struct.pack_into("<H", payload, 16, len(name_bytes))
    payload[18:18 + len(name_bytes)] = name_bytes
    return bytes(payload)


def decode_peer_name_update_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a peer_name_update payload plaintext (post-decryption)."""
    if len(data) != PEER_NAME_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(
            f"peer_name_update plaintext must be {PEER_NAME_UPDATE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    peer_id = data[0:16]
    (name_len,) = struct.unpack_from("<H", data, 16)
    if name_len > NAME_MAX:
        raise ValueError(f"name_len exceeds {NAME_MAX}, got {name_len}")
    name_bytes = data[18:18 + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("name is not valid utf-8") from exc
    return {"peer_id": peer_id, "name": name}


def encode_peer_removed_plaintext(removed_peer_id: bytes) -> bytes:
    """Encode a peer_removed payload plaintext (pre-encryption)."""
    _require_len("removed_peer_id", removed_peer_id, 16)
    payload = bytearray(PEER_REMOVED_PLAINTEXT_SIZE)
    payload[0:16] = removed_peer_id
    return bytes(payload)


def decode_peer_removed_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a peer_removed payload plaintext (post-decryption)."""
    if len(data) != PEER_REMOVED_PLAINTEXT_SIZE:
        raise ValueError(
            f"peer_removed plaintext must be {PEER_REMOVED_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"removed_peer_id": data[0:16]}


def encode_network_plaintext(network_pubkey: bytes) -> bytes:
    """Encode a network payload plaintext (pre-encryption)."""
    _require_len("network_pubkey", network_pubkey, PUBKEY_SIZE)
    payload = bytearray(NETWORK_PLAINTEXT_SIZE)
    payload[0:32] = network_pubkey
    return bytes(payload)


def decode_network_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a network payload plaintext (post-decryption)."""
    if len(data) != NETWORK_PLAINTEXT_SIZE:
        raise ValueError(f"network plaintext must be {NETWORK_PLAINTEXT_SIZE} bytes, got {len(data)}")
    return {"network_pubkey": data[0:32]}


def encode_network_name_update_plaintext(network_id: bytes, name: str | bytes) -> bytes:
    """Encode a network_name_update payload plaintext (pre-encryption)."""
    _require_len("network_id", network_id, 16)
    if isinstance(name, str):
        name_bytes = name.encode("utf-8")
    else:
        name_bytes = bytes(name)
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")
    payload = bytearray(NETWORK_NAME_UPDATE_PLAINTEXT_SIZE)
    payload[0:16] = network_id
    struct.pack_into("<H", payload, 16, len(name_bytes))
    payload[18:18 + len(name_bytes)] = name_bytes
    return bytes(payload)


def decode_network_name_update_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a network_name_update payload plaintext (post-decryption)."""
    if len(data) != NETWORK_NAME_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(
            f"network_name_update plaintext must be {NETWORK_NAME_UPDATE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    network_id = data[0:16]
    (name_len,) = struct.unpack_from("<H", data, 16)
    if name_len > NAME_MAX:
        raise ValueError(f"name_len exceeds {NAME_MAX}, got {name_len}")
    name_bytes = data[18:18 + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("name is not valid utf-8") from exc
    return {"network_id": network_id, "name": name}


def encode_admin_plaintext(
    user_id: bytes,
    network_id: bytes,
    admin_grant_id: bytes | None,
) -> bytes:
    """Encode an admin payload plaintext (pre-encryption)."""
    _require_len("user_id", user_id, 16)
    _require_len("network_id", network_id, 16)
    admin_grant_bytes = admin_grant_id or (b"\x00" * 16)
    _require_len("admin_grant_id", admin_grant_bytes, 16)
    payload = bytearray(ADMIN_PLAINTEXT_SIZE)
    payload[0:16] = user_id
    payload[16:32] = network_id
    payload[32:48] = admin_grant_bytes
    return bytes(payload)


def decode_admin_plaintext(data: bytes) -> dict[str, Any]:
    """Decode an admin payload plaintext (post-decryption)."""
    if len(data) != ADMIN_PLAINTEXT_SIZE:
        raise ValueError(f"admin plaintext must be {ADMIN_PLAINTEXT_SIZE} bytes, got {len(data)}")
    user_id = data[0:16]
    network_id = data[16:32]
    admin_grant_id = data[32:48]
    if admin_grant_id == b"\x00" * 16:
        admin_grant_id = None
    return {"user_id": user_id, "network_id": network_id, "admin_grant_id": admin_grant_id}


def encode_invite_plaintext(
    mode: int,
    invite_pubkey: bytes,
    invite_prekey_id: bytes | None,
    group_id: bytes | None,
    channel_id: bytes | None,
    key_id: bytes | None,
    network_id: bytes | None,
    inviter_peer_shared_id: bytes | None,
    inviter_user_id: bytes | None,
    target_user_id: bytes | None,
    admin_grant_id: bytes | None,
    inviter_ip: str | None,
    inviter_port: int | None,
) -> bytes:
    """Encode an invite payload plaintext (pre-encryption)."""
    if mode not in (INVITE_MODE_USER, INVITE_MODE_PEER):
        raise ValueError(f"invite mode must be {INVITE_MODE_USER} or {INVITE_MODE_PEER}")
    _require_len("invite_pubkey", invite_pubkey, PUBKEY_SIZE)
    invite_prekey_bytes = invite_prekey_id or (b"\x00" * 16)
    _require_len("invite_prekey_id", invite_prekey_bytes, 16)
    group_bytes = group_id or (b"\x00" * 16)
    _require_len("group_id", group_bytes, 16)
    channel_bytes = channel_id or (b"\x00" * 16)
    _require_len("channel_id", channel_bytes, 16)
    key_bytes = key_id or (b"\x00" * 16)
    _require_len("key_id", key_bytes, 16)
    network_bytes = network_id or (b"\x00" * 16)
    _require_len("network_id", network_bytes, 16)
    inviter_peer_bytes = inviter_peer_shared_id or (b"\x00" * 16)
    _require_len("inviter_peer_shared_id", inviter_peer_bytes, 16)
    inviter_user_bytes = inviter_user_id or (b"\x00" * 16)
    _require_len("inviter_user_id", inviter_user_bytes, 16)
    target_user_bytes = target_user_id or (b"\x00" * 16)
    _require_len("target_user_id", target_user_bytes, 16)
    admin_grant_bytes = admin_grant_id or (b"\x00" * 16)
    _require_len("admin_grant_id", admin_grant_bytes, 16)
    if inviter_port is not None and (inviter_port < 0 or inviter_port > 0xFFFF):
        raise ValueError("inviter_port must fit in u16")
    payload = bytearray(INVITE_PLAINTEXT_SIZE)
    payload[0] = mode
    payload[1:33] = invite_pubkey
    payload[33:49] = invite_prekey_bytes
    payload[49:65] = group_bytes
    payload[65:81] = channel_bytes
    payload[81:97] = key_bytes
    payload[97:113] = network_bytes
    payload[113:129] = inviter_peer_bytes
    payload[129:145] = inviter_user_bytes
    payload[145:161] = target_user_bytes
    payload[161:177] = admin_grant_bytes
    payload[177:193] = _encode_ip16(inviter_ip)
    struct.pack_into("<H", payload, 193, inviter_port or 0)
    return bytes(payload)


def decode_invite_plaintext(data: bytes) -> dict[str, Any]:
    """Decode an invite payload plaintext (post-decryption)."""
    if len(data) != INVITE_PLAINTEXT_SIZE:
        raise ValueError(f"invite plaintext must be {INVITE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    mode = data[0]
    invite_pubkey = data[1:33]
    invite_prekey_id = data[33:49]
    group_id = data[49:65]
    channel_id = data[65:81]
    key_id = data[81:97]
    network_id = data[97:113]
    inviter_peer_shared_id = data[113:129]
    inviter_user_id = data[129:145]
    target_user_id = data[145:161]
    admin_grant_id = data[161:177]
    inviter_ip = _decode_ip16(data[177:193])
    (inviter_port,) = struct.unpack_from("<H", data, 193)
    if invite_prekey_id == b"\x00" * 16:
        invite_prekey_id = None
    if group_id == b"\x00" * 16:
        group_id = None
    if channel_id == b"\x00" * 16:
        channel_id = None
    if key_id == b"\x00" * 16:
        key_id = None
    if network_id == b"\x00" * 16:
        network_id = None
    if inviter_peer_shared_id == b"\x00" * 16:
        inviter_peer_shared_id = None
    if inviter_user_id == b"\x00" * 16:
        inviter_user_id = None
    if target_user_id == b"\x00" * 16:
        target_user_id = None
    if admin_grant_id == b"\x00" * 16:
        admin_grant_id = None
    if inviter_port == 0:
        inviter_port = None
    return {
        "mode": mode,
        "invite_pubkey": invite_pubkey,
        "invite_prekey_id": invite_prekey_id,
        "group_id": group_id,
        "channel_id": channel_id,
        "key_id": key_id,
        "network_id": network_id,
        "inviter_peer_shared_id": inviter_peer_shared_id,
        "inviter_user_id": inviter_user_id,
        "target_user_id": target_user_id,
        "admin_grant_id": admin_grant_id,
        "inviter_ip": inviter_ip,
        "inviter_port": inviter_port,
    }


def encode_invite_accepted_plaintext(
    invite_id: bytes,
    invite_prekey_id: bytes | None,
    invite_private_key: bytes,
    inviter_peer_shared_id: bytes | None,
    network_id: bytes | None,
    channel_id: bytes | None,
    key_id: bytes | None,
    inviter_connection_prekey_public_key: bytes | None,
    inviter_connection_prekey_shared_id: bytes | None,
    inviter_connection_prekey_id: bytes | None,
    inviter_ip: str | None,
    inviter_port: int | None,
    link_user_id: bytes | None,
    inviter_peer_shared_blob_id: bytes | None,
) -> bytes:
    """Encode an invite_accepted payload plaintext (pre-encryption)."""
    _require_len("invite_id", invite_id, 16)
    _require_len("invite_private_key", invite_private_key, PRIVKEY_SIZE)
    invite_prekey_bytes = invite_prekey_id or (b"\x00" * 16)
    _require_len("invite_prekey_id", invite_prekey_bytes, 16)
    inviter_peer_bytes = inviter_peer_shared_id or (b"\x00" * 16)
    _require_len("inviter_peer_shared_id", inviter_peer_bytes, 16)
    network_bytes = network_id or (b"\x00" * 16)
    _require_len("network_id", network_bytes, 16)
    channel_bytes = channel_id or (b"\x00" * 16)
    _require_len("channel_id", channel_bytes, 16)
    key_bytes = key_id or (b"\x00" * 16)
    _require_len("key_id", key_bytes, 16)
    inviter_prekey_pub = inviter_connection_prekey_public_key or (b"\x00" * PUBKEY_SIZE)
    _require_len("inviter_connection_prekey_public_key", inviter_prekey_pub, PUBKEY_SIZE)
    inviter_prekey_shared = inviter_connection_prekey_shared_id or (b"\x00" * 16)
    _require_len("inviter_connection_prekey_shared_id", inviter_prekey_shared, 16)
    inviter_prekey_id_bytes = inviter_connection_prekey_id or (b"\x00" * 16)
    _require_len("inviter_connection_prekey_id", inviter_prekey_id_bytes, 16)
    link_user_bytes = link_user_id or (b"\x00" * 16)
    _require_len("link_user_id", link_user_bytes, 16)
    blob_id_bytes = inviter_peer_shared_blob_id or (b"\x00" * 16)
    _require_len("inviter_peer_shared_blob_id", blob_id_bytes, 16)
    if inviter_port is not None and (inviter_port < 0 or inviter_port > 0xFFFF):
        raise ValueError("inviter_port must fit in u16")

    payload = bytearray(INVITE_ACCEPTED_PLAINTEXT_SIZE)
    payload[0:16] = invite_id
    payload[16:32] = invite_prekey_bytes
    payload[32:64] = invite_private_key
    payload[64:80] = inviter_peer_bytes
    payload[80:96] = network_bytes
    payload[96:112] = channel_bytes
    payload[112:128] = key_bytes
    payload[128:160] = inviter_prekey_pub
    payload[160:176] = inviter_prekey_shared
    payload[176:192] = inviter_prekey_id_bytes
    payload[192:208] = _encode_ip16(inviter_ip)
    struct.pack_into("<H", payload, 208, inviter_port or 0)
    payload[210:226] = link_user_bytes
    payload[226:242] = blob_id_bytes
    return bytes(payload)


def decode_invite_accepted_plaintext(data: bytes) -> dict[str, Any]:
    """Decode an invite_accepted payload plaintext (post-decryption)."""
    if len(data) != INVITE_ACCEPTED_PLAINTEXT_SIZE:
        raise ValueError(
            f"invite_accepted plaintext must be {INVITE_ACCEPTED_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    invite_id = data[0:16]
    invite_prekey_id = data[16:32]
    invite_private_key = data[32:64]
    inviter_peer_shared_id = data[64:80]
    network_id = data[80:96]
    channel_id = data[96:112]
    key_id = data[112:128]
    inviter_connection_prekey_public_key = data[128:160]
    inviter_connection_prekey_shared_id = data[160:176]
    inviter_connection_prekey_id = data[176:192]
    inviter_ip = _decode_ip16(data[192:208])
    (inviter_port,) = struct.unpack_from("<H", data, 208)
    link_user_id = data[210:226]
    inviter_peer_shared_blob_id = data[226:242]
    if invite_prekey_id == b"\x00" * 16:
        invite_prekey_id = None
    if inviter_peer_shared_id == b"\x00" * 16:
        inviter_peer_shared_id = None
    if network_id == b"\x00" * 16:
        network_id = None
    if channel_id == b"\x00" * 16:
        channel_id = None
    if key_id == b"\x00" * 16:
        key_id = None
    if inviter_connection_prekey_public_key == b"\x00" * PUBKEY_SIZE:
        inviter_connection_prekey_public_key = None
    if inviter_connection_prekey_shared_id == b"\x00" * 16:
        inviter_connection_prekey_shared_id = None
    if inviter_connection_prekey_id == b"\x00" * 16:
        inviter_connection_prekey_id = None
    if inviter_port == 0:
        inviter_port = None
    if link_user_id == b"\x00" * 16:
        link_user_id = None
    if inviter_peer_shared_blob_id == b"\x00" * 16:
        inviter_peer_shared_blob_id = None
    return {
        "invite_id": invite_id,
        "invite_prekey_id": invite_prekey_id,
        "invite_private_key": invite_private_key,
        "inviter_peer_shared_id": inviter_peer_shared_id,
        "network_id": network_id,
        "channel_id": channel_id,
        "key_id": key_id,
        "inviter_connection_prekey_public_key": inviter_connection_prekey_public_key,
        "inviter_connection_prekey_shared_id": inviter_connection_prekey_shared_id,
        "inviter_connection_prekey_id": inviter_connection_prekey_id,
        "inviter_ip": inviter_ip,
        "inviter_port": inviter_port,
        "link_user_id": link_user_id,
        "inviter_peer_shared_blob_id": inviter_peer_shared_blob_id,
    }


def encode_self_address_plaintext(peer_id: bytes, ip: str, port: int) -> bytes:
    """Encode a self_address payload plaintext (pre-encryption)."""
    _require_len("peer_id", peer_id, 16)
    if port < 0 or port > 0xFFFF:
        raise ValueError("port must fit in u16")
    payload = bytearray(SELF_ADDRESS_PLAINTEXT_SIZE)
    payload[0:16] = peer_id
    payload[16:32] = _encode_ip16(ip)
    struct.pack_into("<H", payload, 32, port)
    return bytes(payload)


def decode_self_address_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a self_address payload plaintext (post-decryption)."""
    if len(data) != SELF_ADDRESS_PLAINTEXT_SIZE:
        raise ValueError(
            f"self_address plaintext must be {SELF_ADDRESS_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    peer_id = data[0:16]
    ip = _decode_ip16(data[16:32])
    (port,) = struct.unpack_from("<H", data, 32)
    return {"peer_id": peer_id, "ip": ip, "port": port}


def encode_observed_address_plaintext(observed_peer_id: bytes, ip: str, port: int) -> bytes:
    """Encode an observed_address payload plaintext (pre-encryption)."""
    _require_len("observed_peer_id", observed_peer_id, 16)
    if port < 0 or port > 0xFFFF:
        raise ValueError("port must fit in u16")
    payload = bytearray(OBSERVED_ADDRESS_PLAINTEXT_SIZE)
    payload[0:16] = observed_peer_id
    payload[16:32] = _encode_ip16(ip)
    struct.pack_into("<H", payload, 32, port)
    return bytes(payload)


def decode_observed_address_plaintext(data: bytes) -> dict[str, Any]:
    """Decode an observed_address payload plaintext (post-decryption)."""
    if len(data) != OBSERVED_ADDRESS_PLAINTEXT_SIZE:
        raise ValueError(
            f"observed_address plaintext must be {OBSERVED_ADDRESS_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    observed_peer_id = data[0:16]
    ip = _decode_ip16(data[16:32])
    (port,) = struct.unpack_from("<H", data, 32)
    return {"observed_peer_id": observed_peer_id, "ip": ip, "port": port}


def encode_network_intro_plaintext(peer1_id: bytes, peer2_id: bytes) -> bytes:
    """Encode a network_intro payload plaintext (pre-encryption)."""
    _require_len("peer1_id", peer1_id, 16)
    _require_len("peer2_id", peer2_id, 16)
    payload = bytearray(NETWORK_INTRO_PLAINTEXT_SIZE)
    payload[0:16] = peer1_id
    payload[16:32] = peer2_id
    return bytes(payload)


def decode_network_intro_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a network_intro payload plaintext (post-decryption)."""
    if len(data) != NETWORK_INTRO_PLAINTEXT_SIZE:
        raise ValueError(
            f"network_intro plaintext must be {NETWORK_INTRO_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"peer1_id": data[0:16], "peer2_id": data[16:32]}


def encode_negentropy_plaintext(
    connection_id: bytes,
    reply_connection_id: bytes,
    msg_type: int,
    range_id: bytes,
    level: int,
    prefix_bytes: bytes,
    hash_bytes: bytes,
    root_hash: bytes,
    total_events: int,
    parent_range_id: bytes,
    event_ids: list[bytes],
) -> bytes:
    """Encode a negentropy payload plaintext (pre-encryption)."""
    _require_len("connection_id", connection_id, 16)
    _require_len("reply_connection_id", reply_connection_id, 16)
    if msg_type not in (NEGENTROPY_MSG_RANGE_REQUEST, NEGENTROPY_MSG_RANGE_MATCHED, NEGENTROPY_MSG_RANGE_EVENTS):
        raise ValueError("invalid negentropy msg_type")
    _require_len("range_id", range_id, NEGENTROPY_RANGE_ID_SIZE)
    if level not in (
        NEGENTROPY_LEVEL_ROOT,
        NEGENTROPY_LEVEL_PREFIX_2,
        NEGENTROPY_LEVEL_PREFIX_4,
        NEGENTROPY_LEVEL_PREFIX_6,
    ):
        raise ValueError("invalid negentropy level")
    if len(prefix_bytes) > NEGENTROPY_PREFIX_BYTES:
        raise ValueError("prefix_bytes exceeds max")
    _require_len("hash_bytes", hash_bytes, 16)
    _require_len("root_hash", root_hash, 16)
    if total_events < 0 or total_events > 0xFFFFFFFF:
        raise ValueError("total_events must fit in u32")
    _require_len("parent_range_id", parent_range_id, NEGENTROPY_RANGE_ID_SIZE)
    if len(event_ids) > NEGENTROPY_EVENT_ID_MAX:
        raise ValueError("event_ids exceeds max")
    for event_id in event_ids:
        _require_len("event_id", event_id, 16)

    payload = bytearray(NEGENTROPY_PLAINTEXT_SIZE)
    payload[0:16] = connection_id
    payload[16:32] = reply_connection_id
    payload[32] = msg_type
    payload[33:41] = range_id
    payload[41] = level
    payload[42] = len(prefix_bytes)
    payload[43:43 + len(prefix_bytes)] = prefix_bytes
    payload[46:62] = hash_bytes
    payload[62:78] = root_hash
    struct.pack_into("<I", payload, 78, total_events)
    payload[82:90] = parent_range_id
    payload[90] = len(event_ids)
    cursor = 91
    for event_id in event_ids:
        payload[cursor:cursor + 16] = event_id
        cursor += 16
    return bytes(payload)


def decode_negentropy_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a negentropy payload plaintext (post-decryption)."""
    if len(data) != NEGENTROPY_PLAINTEXT_SIZE:
        raise ValueError(
            f"negentropy plaintext must be {NEGENTROPY_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    connection_id = data[0:16]
    reply_connection_id = data[16:32]
    msg_type = data[32]
    range_id = data[33:41]
    level = data[41]
    prefix_len = data[42]
    if prefix_len > NEGENTROPY_PREFIX_BYTES:
        raise ValueError("invalid prefix length")
    prefix_bytes = data[43:43 + prefix_len]
    hash_bytes = data[46:62]
    root_hash = data[62:78]
    (total_events,) = struct.unpack_from("<I", data, 78)
    parent_range_id = data[82:90]
    event_count = data[90]
    if event_count > NEGENTROPY_EVENT_ID_MAX:
        raise ValueError("negentropy event_count exceeds max")
    event_ids: list[bytes] = []
    cursor = 91
    for _ in range(event_count):
        event_ids.append(data[cursor:cursor + 16])
        cursor += 16
    return {
        "connection_id": connection_id,
        "reply_connection_id": reply_connection_id,
        "msg_type": msg_type,
        "range_id": range_id,
        "level": level,
        "prefix_bytes": prefix_bytes,
        "hash_bytes": hash_bytes,
        "root_hash": root_hash,
        "total_events": total_events,
        "parent_range_id": parent_range_id,
        "event_ids": event_ids,
    }


def signer_type_from_str(signer_type: str) -> int:
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


def signer_type_to_str(signer_type: int) -> str:
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


def is_wire_message_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_MESSAGE


def is_wire_channel_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_CHANNEL


def is_wire_message_update_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_MESSAGE_UPDATE


def is_wire_message_deletion_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_MESSAGE_DELETION


def is_wire_message_reaction_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_MESSAGE_REACTION


def is_wire_message_reaction_deletion_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_MESSAGE_REACTION_DELETION


def is_wire_message_attachment_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_MESSAGE_ATTACHMENT


def is_wire_message_rekey_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_MESSAGE_REKEY


def is_wire_channel_update_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_CHANNEL_UPDATE


def is_wire_group_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_GROUP


def is_wire_group_member_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_GROUP_MEMBER


def is_wire_group_key_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_GROUP_KEY


def is_wire_group_key_shared_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_GROUP_KEY_SHARED


def is_wire_group_prekey_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_GROUP_PREKEY


def is_wire_group_prekey_shared_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_GROUP_PREKEY_SHARED


def is_wire_connection_prekey_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_CONNECTION_PREKEY


def is_wire_connection_prekey_shared_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_CONNECTION_PREKEY_SHARED


def is_wire_connection_request_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_CONNECTION_REQUEST


def is_wire_connection_ack_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_CONNECTION_ACK


def is_wire_file_slice(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    return data[0] == 1 and data[1] == TYPE_FILE_SLICE


def is_wire_user_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_USER


def is_wire_username_update_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_USERNAME_UPDATE


def is_wire_user_removed_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_USER_REMOVED


def is_wire_peer_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_PEER


def is_wire_peer_shared_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_PEER_SHARED


def is_wire_peer_name_update_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_PEER_NAME_UPDATE


def is_wire_peer_removed_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_PEER_REMOVED


def is_wire_network_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_NETWORK


def is_wire_network_name_update_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_NETWORK_NAME_UPDATE


def is_wire_admin_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_ADMIN


def is_wire_invite_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_INVITE


def is_wire_invite_accepted_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_INVITE_ACCEPTED


def is_wire_self_address_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_SELF_ADDRESS


def is_wire_observed_address_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_OBSERVED_ADDRESS


def is_wire_network_intro_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_NETWORK_INTRO


def is_wire_negentropy_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_NEGENTROPY


def _signing_bytes(header: WireHeader, plaintext: bytes) -> bytes:
    if len(plaintext) > PAYLOAD_SIZE:
        raise ValueError("plaintext exceeds payload size")
    padded = plaintext + (b"\x00" * (PAYLOAD_SIZE - len(plaintext)))
    return header.pack() + padded


def _encrypt_message_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != MESSAGE_PLAINTEXT_SIZE:
        raise ValueError(f"message plaintext must be {MESSAGE_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != MESSAGE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_message_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("message payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_channel_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != CHANNEL_PLAINTEXT_SIZE:
        raise ValueError(f"channel plaintext must be {CHANNEL_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("channel payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != CHANNEL_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for channel payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_channel_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("channel payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_message_update_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != MESSAGE_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(f"message_update plaintext must be {MESSAGE_UPDATE_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_update payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != MESSAGE_UPDATE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_update payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_message_update_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("message_update payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_message_deletion_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != MESSAGE_DELETION_PLAINTEXT_SIZE:
        raise ValueError(f"message_deletion plaintext must be {MESSAGE_DELETION_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_deletion payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != MESSAGE_DELETION_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_deletion payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_message_deletion_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("message_deletion payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_message_reaction_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != MESSAGE_REACTION_PLAINTEXT_SIZE:
        raise ValueError(f"message_reaction plaintext must be {MESSAGE_REACTION_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_reaction payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != MESSAGE_REACTION_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_reaction payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_message_reaction_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("message_reaction payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_message_reaction_deletion_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_reaction_deletion plaintext must be {MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE} bytes"
        )
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_reaction_deletion payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_reaction_deletion payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_message_reaction_deletion_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("message_reaction_deletion payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_message_attachment_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != MESSAGE_ATTACHMENT_PLAINTEXT_SIZE:
        raise ValueError(f"message_attachment plaintext must be {MESSAGE_ATTACHMENT_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_attachment payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != MESSAGE_ATTACHMENT_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_attachment payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_message_attachment_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("message_attachment payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_channel_update_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != CHANNEL_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(f"channel_update plaintext must be {CHANNEL_UPDATE_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("channel_update payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != CHANNEL_UPDATE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for channel_update payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_channel_update_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("channel_update payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_group_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != GROUP_PLAINTEXT_SIZE:
        raise ValueError(f"group plaintext must be {GROUP_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("group payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != GROUP_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for group payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_group_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("group payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_group_member_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != GROUP_MEMBER_PLAINTEXT_SIZE:
        raise ValueError(f"group_member plaintext must be {GROUP_MEMBER_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("group_member payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != GROUP_MEMBER_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for group_member payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_group_member_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("group_member payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_group_key_shared_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != GROUP_KEY_SHARED_PLAINTEXT_SIZE:
        raise ValueError(f"group_key_shared plaintext must be {GROUP_KEY_SHARED_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "asymmetric":
        raise ValueError("group_key_shared payload requires asymmetric key")
    sealed = crypto.seal(plaintext, key_data["public_key"])
    if len(sealed) != PAYLOAD_SIZE - 16:
        raise ValueError("unexpected sealed length for group_key_shared payload")
    payload = key_id + sealed
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_group_key_shared_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "asymmetric":
        raise ValueError("group_key_shared payload requires asymmetric key")
    sealed = payload[16:]
    return crypto.unseal(sealed, key_data["private_key"])


def _encrypt_username_update_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != USERNAME_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(f"username_update plaintext must be {USERNAME_UPDATE_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("username_update payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != USERNAME_UPDATE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for username_update payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_username_update_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("username_update payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_network_name_update_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != NETWORK_NAME_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(
            f"network_name_update plaintext must be {NETWORK_NAME_UPDATE_PLAINTEXT_SIZE} bytes"
        )
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("network_name_update payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != NETWORK_NAME_UPDATE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for network_name_update payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_network_name_update_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("network_name_update payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def _encrypt_peer_name_update_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    if len(plaintext) != PEER_NAME_UPDATE_PLAINTEXT_SIZE:
        raise ValueError(f"peer_name_update plaintext must be {PEER_NAME_UPDATE_PLAINTEXT_SIZE} bytes")
    key_id = _require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("peer_name_update payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != PEER_NAME_UPDATE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for peer_name_update payload")
    payload = key_id + nonce + ciphertext
    return _require_len("payload", payload, PAYLOAD_SIZE)


def _decrypt_peer_name_update_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    if key_data.get("type") != "symmetric":
        raise ValueError("peer_name_update payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def encode_message_wire_event(
    *,
    channel_id_b64: str,
    author_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    content: str,
    created_at_ms: int,
    ttl_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    channel_id = crypto.b64decode(channel_id_b64)
    author_id = crypto.b64decode(author_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_message_plaintext(
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        disappearing_time_ms=ttl_ms,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_MESSAGE,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=ttl_ms,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_message_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_message_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_MESSAGE:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_message_payload(payload, key_data)
    else:
        plaintext = payload[:MESSAGE_PLAINTEXT_SIZE]

    decoded = decode_message_plaintext(plaintext)
    event_data = {
        "type": "message",
        "channel_id": crypto.b64encode(decoded["channel_id"]),
        "author_id": crypto.b64encode(decoded["author_id"]),
        "disappearing_time_ms": decoded["disappearing_time_ms"],
        "content": decoded["content"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_channel_wire_event(
    *,
    group_id_b64: str,
    name: str,
    disappearing_time_ms: int,
    is_main: int | bool,
    admin_grant_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    group_id = crypto.b64decode(group_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    admin_grant_id = crypto.b64decode(admin_grant_b64) if admin_grant_b64 else None

    plaintext = encode_channel_plaintext(
        group_id=group_id,
        name=name,
        disappearing_time_ms=disappearing_time_ms,
        is_main=is_main,
        admin_grant_id=admin_grant_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_CHANNEL,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=disappearing_time_ms,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_channel_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_channel_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_CHANNEL:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_channel_payload(payload, key_data)
    else:
        plaintext = payload[:CHANNEL_PLAINTEXT_SIZE]

    decoded = decode_channel_plaintext(plaintext)
    event_data = {
        "type": "channel",
        "group_id": crypto.b64encode(decoded["group_id"]),
        "name": decoded["name"],
        "disappearing_time_ms": decoded["disappearing_time_ms"],
        "is_main": decoded["is_main"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    if decoded["admin_grant_id"]:
        event_data["admin_grant"] = crypto.b64encode(decoded["admin_grant_id"])
    return event_data, []


def encode_message_update_wire_event(
    *,
    message_id_b64: str,
    group_id_b64: str,
    edited_by_b64: str,
    author_id_b64: str,
    signer_type: str,
    new_content: str,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")

    message_id = crypto.b64decode(message_id_b64)
    group_id = crypto.b64decode(group_id_b64)
    edited_by = crypto.b64decode(edited_by_b64)
    author_id = crypto.b64decode(author_id_b64)

    plaintext = encode_message_update_plaintext(
        message_id=message_id,
        group_id=group_id,
        edited_by=edited_by,
        author_id=author_id,
        new_content=new_content,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_MESSAGE_UPDATE,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", edited_by, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_message_update_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_message_update_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_MESSAGE_UPDATE:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_message_update_payload(payload, key_data)
    else:
        plaintext = payload[:MESSAGE_UPDATE_PLAINTEXT_SIZE]

    decoded = decode_message_update_plaintext(plaintext)
    if decoded["edited_by"] != header.signer_id:
        raise ValueError("edited_by does not match signer_id")
    event_data = {
        "type": "message_update",
        "message_id": crypto.b64encode(decoded["message_id"]),
        "group_id": crypto.b64encode(decoded["group_id"]),
        "edited_by": crypto.b64encode(decoded["edited_by"]),
        "author_id": crypto.b64encode(decoded["author_id"]),
        "new_content": decoded["new_content"],
        "global_count": header.count,
        "created_at": header.created_at_ms,
        "signer_type": signer_type_to_str(header.signer_type),
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_message_deletion_wire_event(
    *,
    message_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    message_id = crypto.b64decode(message_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_message_deletion_plaintext(message_id=message_id)
    header = WireHeader(
        version=1,
        event_type=TYPE_MESSAGE_DELETION,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_message_deletion_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_message_deletion_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_MESSAGE_DELETION:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_message_deletion_payload(payload, key_data)
    else:
        plaintext = payload[:MESSAGE_DELETION_PLAINTEXT_SIZE]

    decoded = decode_message_deletion_plaintext(plaintext)
    event_data = {
        "type": "message_deletion",
        "message_id": crypto.b64encode(decoded["message_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_message_reaction_wire_event(
    *,
    message_id_b64: str,
    reactor_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    emoji: str,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")
    message_id = crypto.b64decode(message_id_b64)
    reactor_id = crypto.b64decode(reactor_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_message_reaction_plaintext(
        message_id=message_id,
        reactor_id=reactor_id,
        emoji=emoji,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_MESSAGE_REACTION,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_message_reaction_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_message_reaction_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_MESSAGE_REACTION:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_message_reaction_payload(payload, key_data)
    else:
        plaintext = payload[:MESSAGE_REACTION_PLAINTEXT_SIZE]

    decoded = decode_message_reaction_plaintext(plaintext)
    event_data = {
        "type": "message_reaction",
        "message_id": crypto.b64encode(decoded["message_id"]),
        "reactor_id": crypto.b64encode(decoded["reactor_id"]),
        "emoji": decoded["emoji"],
        "global_count": header.count,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_message_reaction_deletion_wire_event(
    *,
    reaction_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    reaction_id = crypto.b64decode(reaction_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_message_reaction_deletion_plaintext(reaction_id=reaction_id)
    header = WireHeader(
        version=1,
        event_type=TYPE_MESSAGE_REACTION_DELETION,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_message_reaction_deletion_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_message_reaction_deletion_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_MESSAGE_REACTION_DELETION:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_message_reaction_deletion_payload(payload, key_data)
    else:
        plaintext = payload[:MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE]

    decoded = decode_message_reaction_deletion_plaintext(plaintext)
    event_data = {
        "type": "message_reaction_deletion",
        "reaction_id": crypto.b64encode(decoded["reaction_id"]),
        "deleted_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_message_attachment_wire_event(
    *,
    message_id_b64: str,
    file_id_b64: str,
    blob_bytes: int,
    total_slices: int,
    nonce_prefix_b64: str,
    enc_key_b64: str,
    root_hash_b64: str,
    filename: str | None,
    mime_type: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    message_id = crypto.b64decode(message_id_b64)
    file_id = crypto.b64decode(file_id_b64)
    nonce_prefix = crypto.b64decode(nonce_prefix_b64)
    enc_key = crypto.b64decode(enc_key_b64)
    root_hash = crypto.b64decode(root_hash_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_message_attachment_plaintext(
        message_id=message_id,
        file_id=file_id,
        blob_bytes=blob_bytes,
        total_slices=total_slices,
        nonce_prefix=nonce_prefix,
        enc_key=enc_key,
        root_hash=root_hash,
        filename=filename,
        mime_type=mime_type,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_MESSAGE_ATTACHMENT,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_message_attachment_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_message_attachment_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_MESSAGE_ATTACHMENT:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_message_attachment_payload(payload, key_data)
    else:
        plaintext = payload[:MESSAGE_ATTACHMENT_PLAINTEXT_SIZE]

    decoded = decode_message_attachment_plaintext(plaintext)
    event_data = {
        "type": "message_attachment",
        "message_id": crypto.b64encode(decoded["message_id"]),
        "file_id": crypto.b64encode(decoded["file_id"]),
        "filename": decoded["filename"],
        "mime_type": decoded["mime_type"],
        "blob_bytes": decoded["blob_bytes"],
        "total_slices": decoded["total_slices"],
        "nonce_prefix": crypto.b64encode(decoded["nonce_prefix"]),
        "enc_key": crypto.b64encode(decoded["enc_key"]),
        "root_hash": crypto.b64encode(decoded["root_hash"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_message_rekey_wire_event(
    *,
    original_message_id_b64: str,
    new_key_id_b64: str,
    new_ciphertext: bytes,
    signed_by_b64: str,
    created_at_ms: int,
) -> bytes:
    original_message_id = crypto.b64decode(original_message_id_b64)
    new_key_id = crypto.b64decode(new_key_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_message_rekey_plaintext(
        original_message_id=original_message_id,
        new_key_id=new_key_id,
        new_ciphertext=new_ciphertext,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_MESSAGE_REKEY,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_PEER,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_message_rekey_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_MESSAGE_REKEY:
        raise ValueError("unexpected event type for message_rekey")
    plaintext = payload[:MESSAGE_REKEY_PLAINTEXT_SIZE]
    decoded = decode_message_rekey_plaintext(plaintext)
    return {
        "type": "message_rekey",
        "original_message_id": crypto.b64encode(decoded["original_message_id"]),
        "new_key_id": crypto.b64encode(decoded["new_key_id"]),
        "new_ciphertext": crypto.b64encode(decoded["new_ciphertext"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_ciphertext_only": True,
    }


def encode_channel_update_wire_event(
    *,
    channel_id_b64: str,
    group_id_b64: str,
    updated_by_b64: str,
    signer_type: str,
    new_channel_name: str | None,
    new_disappearing_time_ms: int | None,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")
    channel_id = crypto.b64decode(channel_id_b64)
    group_id = crypto.b64decode(group_id_b64)
    updated_by = crypto.b64decode(updated_by_b64)
    plaintext = encode_channel_update_plaintext(
        channel_id=channel_id,
        group_id=group_id,
        updated_by=updated_by,
        new_channel_name=new_channel_name,
        new_disappearing_time_ms=new_disappearing_time_ms,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_CHANNEL_UPDATE,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", updated_by, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_channel_update_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_channel_update_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_CHANNEL_UPDATE:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_channel_update_payload(payload, key_data)
    else:
        plaintext = payload[:CHANNEL_UPDATE_PLAINTEXT_SIZE]
    decoded = decode_channel_update_plaintext(plaintext)
    if decoded["updated_by"] != header.signer_id:
        raise ValueError("updated_by does not match signer_id")
    event_data = {
        "type": "channel_update",
        "channel_id": crypto.b64encode(decoded["channel_id"]),
        "group_id": crypto.b64encode(decoded["group_id"]),
        "updated_by": crypto.b64encode(decoded["updated_by"]),
        "new_channel_name": decoded["new_channel_name"],
        "new_disappearing_time_ms": decoded["new_disappearing_time_ms"],
        "global_count": header.count,
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_group_wire_event(
    *,
    name: str,
    key_id_b64: str,
    is_main: int | bool,
    network_id_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    key_id = crypto.b64decode(key_id_b64)
    network_id = crypto.b64decode(network_id_b64) if network_id_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_group_plaintext(
        name=name,
        key_id=key_id,
        is_main=is_main,
        network_id=network_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_GROUP,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_group_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_group_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_GROUP:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_group_payload(payload, key_data)
    else:
        plaintext = payload[:GROUP_PLAINTEXT_SIZE]
    decoded = decode_group_plaintext(plaintext)
    event_data = {
        "type": "group",
        "name": decoded["name"],
        "key_id": crypto.b64encode(decoded["key_id"]),
        "is_main": decoded["is_main"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    if decoded["network_id"]:
        event_data["network_id"] = crypto.b64encode(decoded["network_id"])
    return event_data, []


def encode_group_member_wire_event(
    *,
    group_id_b64: str,
    user_id_b64: str,
    added_by_b64: str,
    admin_grant_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    group_id = crypto.b64decode(group_id_b64)
    user_id = crypto.b64decode(user_id_b64)
    added_by = crypto.b64decode(added_by_b64)
    admin_grant_id = crypto.b64decode(admin_grant_b64) if admin_grant_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_group_member_plaintext(
        group_id=group_id,
        user_id=user_id,
        added_by=added_by,
        admin_grant_id=admin_grant_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_GROUP_MEMBER,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_group_member_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_group_member_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_GROUP_MEMBER:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_group_member_payload(payload, key_data)
    else:
        plaintext = payload[:GROUP_MEMBER_PLAINTEXT_SIZE]
    decoded = decode_group_member_plaintext(plaintext)
    if decoded["added_by"] != header.signer_id:
        raise ValueError("added_by does not match signer_id")
    event_data = {
        "type": "group_member",
        "group_id": crypto.b64encode(decoded["group_id"]),
        "user_id": crypto.b64encode(decoded["user_id"]),
        "added_by": crypto.b64encode(decoded["added_by"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    if decoded["admin_grant_id"]:
        event_data["admin_grant"] = crypto.b64encode(decoded["admin_grant_id"])
    return event_data, []


def encode_group_key_wire_event(*, key: bytes, created_at_ms: int) -> bytes:
    plaintext = encode_group_key_plaintext(key=key)
    header = WireHeader(
        version=1,
        event_type=TYPE_GROUP_KEY,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * SIGNER_ID_SIZE,
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_group_key_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_GROUP_KEY:
        raise ValueError("unexpected event type for group_key")
    plaintext = payload[:GROUP_KEY_PLAINTEXT_SIZE]
    decoded = decode_group_key_plaintext(plaintext)
    return {
        "type": "group_key",
        "key": crypto.b64encode(decoded["key"]),
        "created_at": header.created_at_ms,
    }


def encode_group_key_shared_wire_event(
    *,
    key_id_b64: str,
    symmetric_key_b64: str,
    recipient_prekey_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    recipient_prekey: dict[str, Any],
    private_key: bytes,
) -> bytes:
    key_id = crypto.b64decode(key_id_b64)
    symmetric_key = crypto.b64decode(symmetric_key_b64)
    recipient_prekey_id = crypto.b64decode(recipient_prekey_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_group_key_shared_plaintext(
        key_id=key_id,
        symmetric_key=symmetric_key,
        recipient_prekey_id=recipient_prekey_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_GROUP_KEY_SHARED,
        flags=FLAG_ENCRYPTED | FLAG_WRAP_ASYM,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_group_key_shared_payload(plaintext, recipient_prekey)
    return build_envelope(header, payload, signature)


def decode_group_key_shared_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_GROUP_KEY_SHARED:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_event_key_by_id(key_id, recorded_by, db)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_group_key_shared_payload(payload, key_data)
    else:
        plaintext = payload[:GROUP_KEY_SHARED_PLAINTEXT_SIZE]
    decoded = decode_group_key_shared_plaintext(plaintext)
    event_data = {
        "type": "group_key_shared",
        "key_id": crypto.b64encode(decoded["key_id"]),
        "symmetric_key": crypto.b64encode(decoded["symmetric_key"]),
        "recipient_prekey_id": crypto.b64encode(decoded["recipient_prekey_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_group_prekey_wire_event(*, public_key: bytes, private_key: bytes, created_at_ms: int) -> bytes:
    plaintext = encode_group_prekey_plaintext(public_key=public_key, private_key=private_key)
    header = WireHeader(
        version=1,
        event_type=TYPE_GROUP_PREKEY,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * SIGNER_ID_SIZE,
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_group_prekey_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_GROUP_PREKEY:
        raise ValueError("unexpected event type for group_prekey")
    plaintext = payload[:GROUP_PREKEY_PLAINTEXT_SIZE]
    decoded = decode_group_prekey_plaintext(plaintext)
    return {
        "type": "group_prekey",
        "public_key": crypto.b64encode(decoded["public_key"]),
        "private_key": crypto.b64encode(decoded["private_key"]),
        "created_at": header.created_at_ms,
    }


def encode_group_prekey_shared_wire_event(
    *,
    group_prekey_id_b64: str,
    peer_id_b64: str,
    public_key_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    group_prekey_id = crypto.b64decode(group_prekey_id_b64)
    peer_id = crypto.b64decode(peer_id_b64)
    public_key = crypto.b64decode(public_key_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_group_prekey_shared_plaintext(
        group_prekey_id=group_prekey_id,
        peer_id=peer_id,
        public_key=public_key,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_GROUP_PREKEY_SHARED,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_group_prekey_shared_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_GROUP_PREKEY_SHARED:
        raise ValueError("unexpected event type for group_prekey_shared")
    plaintext = payload[:GROUP_PREKEY_SHARED_PLAINTEXT_SIZE]
    decoded = decode_group_prekey_shared_plaintext(plaintext)
    return {
        "type": "group_prekey_shared",
        "group_prekey_id": crypto.b64encode(decoded["group_prekey_id"]),
        "peer_id": crypto.b64encode(decoded["peer_id"]),
        "public_key": crypto.b64encode(decoded["public_key"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


def encode_connection_prekey_wire_event(
    *,
    public_key: bytes,
    private_key: bytes,
    signed_by_b64: str,
    created_at_ms: int,
) -> bytes:
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_connection_prekey_plaintext(public_key=public_key, private_key=private_key)
    header = WireHeader(
        version=1,
        event_type=TYPE_CONNECTION_PREKEY,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_PEER,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_connection_prekey_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_CONNECTION_PREKEY:
        raise ValueError("unexpected event type for connection_prekey")
    plaintext = payload[:CONNECTION_PREKEY_PLAINTEXT_SIZE]
    decoded = decode_connection_prekey_plaintext(plaintext)
    return {
        "type": "connection_prekey",
        "public_key": crypto.b64encode(decoded["public_key"]),
        "private_key": crypto.b64encode(decoded["private_key"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
    }


def encode_connection_prekey_shared_wire_event(
    *,
    connection_prekey_id_b64: str,
    peer_id_b64: str,
    public_key_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    connection_prekey_id = crypto.b64decode(connection_prekey_id_b64)
    peer_id = crypto.b64decode(peer_id_b64)
    public_key = crypto.b64decode(public_key_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_connection_prekey_shared_plaintext(
        connection_prekey_id=connection_prekey_id,
        peer_id=peer_id,
        public_key=public_key,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_CONNECTION_PREKEY_SHARED,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_connection_prekey_shared_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_CONNECTION_PREKEY_SHARED:
        raise ValueError("unexpected event type for connection_prekey_shared")
    plaintext = payload[:CONNECTION_PREKEY_SHARED_PLAINTEXT_SIZE]
    decoded = decode_connection_prekey_shared_plaintext(plaintext)
    return {
        "type": "connection_prekey_shared",
        "connection_prekey_id": crypto.b64encode(decoded["connection_prekey_id"]),
        "peer_id": crypto.b64encode(decoded["peer_id"]),
        "public_key": crypto.b64encode(decoded["public_key"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


def encode_connection_request_wire_event(
    *,
    key: bytes,
    to_peer_shared_id_b64: str | None,
    invite_id_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    ttl_ms: int,
    private_key: bytes,
) -> bytes:
    to_peer_shared_id = crypto.b64decode(to_peer_shared_id_b64) if to_peer_shared_id_b64 else None
    invite_id = crypto.b64decode(invite_id_b64) if invite_id_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_connection_request_plaintext(
        key=key,
        to_peer_shared_id=to_peer_shared_id,
        invite_id=invite_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_CONNECTION_REQUEST,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=ttl_ms,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_connection_request_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_CONNECTION_REQUEST:
        raise ValueError("unexpected event type for connection_request")
    plaintext = payload[:CONNECTION_REQUEST_PLAINTEXT_SIZE]
    decoded = decode_connection_request_plaintext(plaintext)
    event_data = {
        "type": "connection_request",
        "key": crypto.b64encode(decoded["key"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "ttl_ms": header.ttl_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    if decoded["to_peer_shared_id"]:
        event_data["to_peer_shared_id"] = crypto.b64encode(decoded["to_peer_shared_id"])
    if decoded["invite_id"]:
        event_data["invite_id"] = crypto.b64encode(decoded["invite_id"])
    return event_data


def encode_connection_ack_wire_event(
    *,
    for_request_id_b64: str,
    key: bytes,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    ttl_ms: int,
    private_key: bytes,
) -> bytes:
    for_request_id = crypto.b64decode(for_request_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_connection_ack_plaintext(for_request_id=for_request_id, key=key)
    header = WireHeader(
        version=1,
        event_type=TYPE_CONNECTION_ACK,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=ttl_ms,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_connection_ack_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_CONNECTION_ACK:
        raise ValueError("unexpected event type for connection_ack")
    plaintext = payload[:CONNECTION_ACK_PLAINTEXT_SIZE]
    decoded = decode_connection_ack_plaintext(plaintext)
    return {
        "type": "connection_ack",
        "for_request_id": crypto.b64encode(decoded["for_request_id"]),
        "key": crypto.b64encode(decoded["key"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "ttl_ms": header.ttl_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


def encode_file_slice_wire_event(
    *,
    file_id: bytes,
    slice_number: int,
    nonce: bytes,
    ciphertext: bytes,
    poly_tag: bytes,
) -> bytes:
    _require_len("file_id", file_id, 16)
    if slice_number < 0 or slice_number > 0xFFFFFFFF:
        raise ValueError("slice_number must fit in u32")
    _require_len("nonce", nonce, FILE_SLICE_NONCE_SIZE)
    _require_len("poly_tag", poly_tag, FILE_SLICE_TAG_SIZE)
    ciphertext = bytes(ciphertext)
    if len(ciphertext) > FILE_SLICE_CIPHERTEXT_SIZE:
        raise ValueError(
            f"ciphertext exceeds {FILE_SLICE_CIPHERTEXT_SIZE} bytes, got {len(ciphertext)}"
        )
    padded_ciphertext = ciphertext + (b"\x00" * (FILE_SLICE_CIPHERTEXT_SIZE - len(ciphertext)))
    blob = bytearray(WIRE_SIZE)
    blob[0] = 1
    blob[1] = TYPE_FILE_SLICE
    blob[2:18] = file_id
    struct.pack_into("<I", blob, 18, slice_number)
    blob[22:46] = nonce
    blob[46:46 + FILE_SLICE_CIPHERTEXT_SIZE] = padded_ciphertext
    blob[496:512] = poly_tag
    return bytes(blob)


def decode_file_slice_wire_event(data: bytes) -> dict[str, Any]:
    if len(data) != WIRE_SIZE:
        raise ValueError(f"file_slice must be {WIRE_SIZE} bytes, got {len(data)}")
    if data[0] != 1 or data[1] != TYPE_FILE_SLICE:
        raise ValueError("unexpected version or type for file_slice")
    file_id = data[2:18]
    (slice_number,) = struct.unpack_from("<I", data, 18)
    nonce = data[22:46]
    ciphertext = data[46:46 + FILE_SLICE_CIPHERTEXT_SIZE]
    poly_tag = data[496:512]
    return {
        "type": "file_slice",
        "file_id": crypto.b64encode(file_id),
        "slice_number": slice_number,
        "nonce": crypto.b64encode(nonce),
        "ciphertext": crypto.b64encode(ciphertext),
        "poly_tag": crypto.b64encode(poly_tag),
    }


def encode_user_wire_event(
    *,
    invite_id_b64: str,
    user_pubkey_b64: str,
    network_id_b64: str | None,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    invite_id = crypto.b64decode(invite_id_b64)
    user_pubkey = crypto.b64decode(user_pubkey_b64)
    network_id = crypto.b64decode(network_id_b64) if network_id_b64 else None

    plaintext = encode_user_plaintext(
        invite_id=invite_id,
        user_pubkey=user_pubkey,
        network_id=network_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_USER,
        flags=0,
        signer_type=SIGNER_INVITE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", invite_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_user_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_USER:
        raise ValueError("unexpected event type for user")
    plaintext = payload[:USER_PLAINTEXT_SIZE]
    decoded = decode_user_plaintext(plaintext)
    event_data = {
        "type": "user",
        "invite_id": crypto.b64encode(decoded["invite_id"]),
        "signed_by": crypto.b64encode(decoded["invite_id"]),
        "signer_type": "invite",
        "user_pubkey": crypto.b64encode(decoded["user_pubkey"]),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    if decoded["network_id"]:
        event_data["network_id"] = crypto.b64encode(decoded["network_id"])
    return event_data


def encode_username_update_wire_event(
    *,
    user_id_b64: str,
    name: str,
    signed_by_b64: str,
    signer_type: str,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")
    user_id = crypto.b64decode(user_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_username_update_plaintext(user_id=user_id, name=name)
    header = WireHeader(
        version=1,
        event_type=TYPE_USERNAME_UPDATE,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_username_update_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_username_update_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_USERNAME_UPDATE:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_username_update_payload(payload, key_data)
    else:
        plaintext = payload[:USERNAME_UPDATE_PLAINTEXT_SIZE]

    decoded = decode_username_update_plaintext(plaintext)
    event_data = {
        "type": "username_update",
        "user_id": crypto.b64encode(decoded["user_id"]),
        "name": decoded["name"],
        "key_id": crypto.b64encode(payload[:16]),
        "global_count": header.count,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_user_removed_wire_event(
    *,
    removed_user_id_b64: str,
    removed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    removed_user_id = crypto.b64decode(removed_user_id_b64)
    signer_id = crypto.b64decode(removed_by_b64)
    plaintext = encode_user_removed_plaintext(removed_user_id=removed_user_id)
    header = WireHeader(
        version=1,
        event_type=TYPE_USER_REMOVED,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_user_removed_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_USER_REMOVED:
        raise ValueError("unexpected event type for user_removed")
    plaintext = payload[:USER_REMOVED_PLAINTEXT_SIZE]
    decoded = decode_user_removed_plaintext(plaintext)
    event_data = {
        "type": "user_removed",
        "removed_user_id": crypto.b64encode(decoded["removed_user_id"]),
        "removed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data


def encode_peer_wire_event(
    *,
    public_key: bytes,
    private_key: bytes,
    created_at_ms: int,
) -> bytes:
    plaintext = encode_peer_plaintext(public_key=public_key, private_key=private_key)
    header = WireHeader(
        version=1,
        event_type=TYPE_PEER,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * SIGNER_ID_SIZE,
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_peer_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_PEER:
        raise ValueError("unexpected event type for peer")
    plaintext = payload[:PEER_PLAINTEXT_SIZE]
    decoded = decode_peer_plaintext(plaintext)
    return {
        "type": "peer",
        "public_key": crypto.b64encode(decoded["public_key"]),
        "private_key": crypto.b64encode(decoded["private_key"]),
        "created_at": header.created_at_ms,
    }


def encode_peer_shared_wire_event(
    *,
    public_key_b64: str,
    peer_id_b64: str,
    invite_id_b64: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    public_key = crypto.b64decode(public_key_b64)
    peer_id = crypto.b64decode(peer_id_b64)
    invite_id = crypto.b64decode(invite_id_b64)
    plaintext = encode_peer_shared_plaintext(
        public_key=public_key,
        peer_id=peer_id,
        invite_id=invite_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_PEER_SHARED,
        flags=0,
        signer_type=SIGNER_INVITE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", invite_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_peer_shared_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_PEER_SHARED:
        raise ValueError("unexpected event type for peer_shared")
    plaintext = payload[:PEER_SHARED_PLAINTEXT_SIZE]
    decoded = decode_peer_shared_plaintext(plaintext)
    event_data = {
        "type": "peer_shared",
        "public_key": crypto.b64encode(decoded["public_key"]),
        "peer_id": crypto.b64encode(decoded["peer_id"]),
        "created_at": header.created_at_ms,
    }
    if decoded["invite_id"]:
        invite_id_b64 = crypto.b64encode(decoded["invite_id"])
        event_data["invite_id"] = invite_id_b64
        event_data["signed_by"] = invite_id_b64
        event_data["signer_type"] = signer_type_to_str(header.signer_type)
        event_data["_wire_signature"] = signature
        event_data["_wire_signed_bytes"] = _signing_bytes(header, plaintext)
    return event_data


def encode_peer_name_update_wire_event(
    *,
    peer_id_b64: str,
    name: str,
    signed_by_b64: str,
    signer_type: str,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")
    peer_id = crypto.b64decode(peer_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_peer_name_update_plaintext(peer_id=peer_id, name=name)
    header = WireHeader(
        version=1,
        event_type=TYPE_PEER_NAME_UPDATE,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_peer_name_update_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_peer_name_update_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_PEER_NAME_UPDATE:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_peer_name_update_payload(payload, key_data)
    else:
        plaintext = payload[:PEER_NAME_UPDATE_PLAINTEXT_SIZE]

    decoded = decode_peer_name_update_plaintext(plaintext)
    event_data = {
        "type": "peer_name_update",
        "peer_id": crypto.b64encode(decoded["peer_id"]),
        "name": decoded["name"],
        "key_id": crypto.b64encode(payload[:16]),
        "global_count": header.count,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_peer_removed_wire_event(
    *,
    removed_peer_id_b64: str,
    removed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    removed_peer_id = crypto.b64decode(removed_peer_id_b64)
    signer_id = crypto.b64decode(removed_by_b64)
    plaintext = encode_peer_removed_plaintext(removed_peer_id=removed_peer_id)
    header = WireHeader(
        version=1,
        event_type=TYPE_PEER_REMOVED,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_peer_removed_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_PEER_REMOVED:
        raise ValueError("unexpected event type for peer_removed")
    plaintext = payload[:PEER_REMOVED_PLAINTEXT_SIZE]
    decoded = decode_peer_removed_plaintext(plaintext)
    event_data = {
        "type": "peer_removed",
        "removed_peer_shared_id": crypto.b64encode(decoded["removed_peer_id"]),
        "removed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data


def encode_network_wire_event(
    *,
    network_pubkey: bytes,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    plaintext = encode_network_plaintext(network_pubkey=network_pubkey)
    header = WireHeader(
        version=1,
        event_type=TYPE_NETWORK,
        flags=0,
        signer_type=SIGNER_NETWORK,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * SIGNER_ID_SIZE,
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_network_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_NETWORK:
        raise ValueError("unexpected event type for network")
    plaintext = payload[:NETWORK_PLAINTEXT_SIZE]
    decoded = decode_network_plaintext(plaintext)
    return {
        "type": "network",
        "network_pubkey": crypto.b64encode(decoded["network_pubkey"]),
        "signer_type": "network",
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


def encode_network_name_update_wire_event(
    *,
    network_id_b64: str,
    name: str,
    signed_by_b64: str,
    signer_type: str,
    global_count: int,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    if global_count < 0 or global_count > 0xFFFFFFFF:
        raise ValueError("global_count must fit in u32")
    network_id = crypto.b64decode(network_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_network_name_update_plaintext(network_id=network_id, name=name)
    header = WireHeader(
        version=1,
        event_type=TYPE_NETWORK_NAME_UPDATE,
        flags=FLAG_ENCRYPTED,
        signer_type=signer_type_from_str(signer_type),
        count=global_count,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_network_name_update_payload(plaintext, key_data)
    return build_envelope(header, payload, signature)


def decode_network_name_update_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_NETWORK_NAME_UPDATE:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_network_name_update_payload(payload, key_data)
    else:
        plaintext = payload[:NETWORK_NAME_UPDATE_PLAINTEXT_SIZE]

    decoded = decode_network_name_update_plaintext(plaintext)
    event_data = {
        "type": "network_name_update",
        "network_id": crypto.b64encode(decoded["network_id"]),
        "name": decoded["name"],
        "key_id": crypto.b64encode(payload[:16]),
        "global_count": header.count,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    return event_data, []


def encode_admin_wire_event(
    *,
    user_id_b64: str,
    network_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    admin_grant_id_b64: str | None,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    user_id = crypto.b64decode(user_id_b64)
    network_id = crypto.b64decode(network_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    admin_grant_id = crypto.b64decode(admin_grant_id_b64) if admin_grant_id_b64 else None
    plaintext = encode_admin_plaintext(
        user_id=user_id,
        network_id=network_id,
        admin_grant_id=admin_grant_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_ADMIN,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_admin_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_ADMIN:
        raise ValueError("unexpected event type for admin")
    plaintext = payload[:ADMIN_PLAINTEXT_SIZE]
    decoded = decode_admin_plaintext(plaintext)
    event_data = {
        "type": "admin",
        "user_id": crypto.b64encode(decoded["user_id"]),
        "network_id": crypto.b64encode(decoded["network_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    if decoded["admin_grant_id"]:
        event_data["admin_grant"] = crypto.b64encode(decoded["admin_grant_id"])
    return event_data


def encode_invite_wire_event(
    *,
    mode: str,
    invite_pubkey_b64: str,
    invite_prekey_id_b64: str | None,
    group_id_b64: str | None,
    channel_id_b64: str | None,
    key_id_b64: str | None,
    network_id_b64: str | None,
    inviter_peer_shared_id_b64: str | None,
    inviter_user_id_b64: str | None,
    target_user_id_b64: str | None,
    admin_grant_id_b64: str | None,
    inviter_ip: str | None,
    inviter_port: int | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    if mode == "user":
        mode_value = INVITE_MODE_USER
    elif mode == "peer":
        mode_value = INVITE_MODE_PEER
    else:
        raise ValueError("invite mode must be 'user' or 'peer'")

    invite_pubkey = crypto.b64decode(invite_pubkey_b64)
    invite_prekey_id = crypto.b64decode(invite_prekey_id_b64) if invite_prekey_id_b64 else None
    group_id = crypto.b64decode(group_id_b64) if group_id_b64 else None
    channel_id = crypto.b64decode(channel_id_b64) if channel_id_b64 else None
    key_id = crypto.b64decode(key_id_b64) if key_id_b64 else None
    network_id = crypto.b64decode(network_id_b64) if network_id_b64 else None
    inviter_peer_shared_id = (
        crypto.b64decode(inviter_peer_shared_id_b64) if inviter_peer_shared_id_b64 else None
    )
    inviter_user_id = crypto.b64decode(inviter_user_id_b64) if inviter_user_id_b64 else None
    target_user_id = crypto.b64decode(target_user_id_b64) if target_user_id_b64 else None
    admin_grant_id = crypto.b64decode(admin_grant_id_b64) if admin_grant_id_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_invite_plaintext(
        mode=mode_value,
        invite_pubkey=invite_pubkey,
        invite_prekey_id=invite_prekey_id,
        group_id=group_id,
        channel_id=channel_id,
        key_id=key_id,
        network_id=network_id,
        inviter_peer_shared_id=inviter_peer_shared_id,
        inviter_user_id=inviter_user_id,
        target_user_id=target_user_id,
        admin_grant_id=admin_grant_id,
        inviter_ip=inviter_ip,
        inviter_port=inviter_port,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_INVITE,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_invite_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_INVITE:
        raise ValueError("unexpected event type for invite")
    plaintext = payload[:INVITE_PLAINTEXT_SIZE]
    decoded = decode_invite_plaintext(plaintext)

    if decoded["mode"] == INVITE_MODE_USER:
        mode = "user"
    elif decoded["mode"] == INVITE_MODE_PEER:
        mode = "peer"
    else:
        raise ValueError("invalid invite mode")

    event_data = {
        "type": "invite",
        "mode": mode,
        "invite_pubkey": crypto.b64encode(decoded["invite_pubkey"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }
    if decoded["invite_prekey_id"]:
        event_data["invite_prekey_id"] = crypto.b64encode(decoded["invite_prekey_id"])
    if decoded["group_id"]:
        event_data["group_id"] = crypto.b64encode(decoded["group_id"])
    if decoded["channel_id"]:
        event_data["channel_id"] = crypto.b64encode(decoded["channel_id"])
    if decoded["key_id"]:
        event_data["key_id"] = crypto.b64encode(decoded["key_id"])
    if decoded["network_id"]:
        event_data["network_id"] = crypto.b64encode(decoded["network_id"])
    if decoded["inviter_peer_shared_id"]:
        event_data["inviter_peer_shared_id"] = crypto.b64encode(decoded["inviter_peer_shared_id"])
    if decoded["inviter_user_id"]:
        event_data["inviter_user_id"] = crypto.b64encode(decoded["inviter_user_id"])
    if decoded["target_user_id"]:
        event_data["user_id"] = crypto.b64encode(decoded["target_user_id"])
    if decoded["admin_grant_id"]:
        event_data["admin_grant"] = crypto.b64encode(decoded["admin_grant_id"])
    if decoded["inviter_ip"] is not None:
        event_data["address"] = decoded["inviter_ip"]
    if decoded["inviter_port"] is not None:
        event_data["port"] = decoded["inviter_port"]
    return event_data


def encode_invite_accepted_wire_event(
    *,
    invite_id_b64: str,
    invite_prekey_id_b64: str | None,
    invite_private_key: bytes,
    inviter_peer_shared_id_b64: str | None,
    network_id_b64: str | None,
    channel_id_b64: str | None,
    key_id_b64: str | None,
    inviter_connection_prekey_public_key_b64: str | None,
    inviter_connection_prekey_shared_id_b64: str | None,
    inviter_connection_prekey_id_b64: str | None,
    inviter_ip: str | None,
    inviter_port: int | None,
    link_user_id_b64: str | None,
    inviter_peer_shared_blob_id_b64: str | None,
    created_at_ms: int,
    signed_by_b64: str | None,
) -> bytes:
    invite_id = crypto.b64decode(invite_id_b64)
    invite_prekey_id = crypto.b64decode(invite_prekey_id_b64) if invite_prekey_id_b64 else None
    inviter_peer_shared_id = (
        crypto.b64decode(inviter_peer_shared_id_b64) if inviter_peer_shared_id_b64 else None
    )
    network_id = crypto.b64decode(network_id_b64) if network_id_b64 else None
    channel_id = crypto.b64decode(channel_id_b64) if channel_id_b64 else None
    key_id = crypto.b64decode(key_id_b64) if key_id_b64 else None
    inviter_connection_prekey_public_key = (
        crypto.b64decode(inviter_connection_prekey_public_key_b64)
        if inviter_connection_prekey_public_key_b64
        else None
    )
    inviter_connection_prekey_shared_id = (
        crypto.b64decode(inviter_connection_prekey_shared_id_b64)
        if inviter_connection_prekey_shared_id_b64
        else None
    )
    inviter_connection_prekey_id = (
        crypto.b64decode(inviter_connection_prekey_id_b64) if inviter_connection_prekey_id_b64 else None
    )
    link_user_id = crypto.b64decode(link_user_id_b64) if link_user_id_b64 else None
    inviter_peer_shared_blob_id = (
        crypto.b64decode(inviter_peer_shared_blob_id_b64) if inviter_peer_shared_blob_id_b64 else None
    )
    signer_id = crypto.b64decode(signed_by_b64) if signed_by_b64 else (b"\x00" * SIGNER_ID_SIZE)

    plaintext = encode_invite_accepted_plaintext(
        invite_id=invite_id,
        invite_prekey_id=invite_prekey_id,
        invite_private_key=invite_private_key,
        inviter_peer_shared_id=inviter_peer_shared_id,
        network_id=network_id,
        channel_id=channel_id,
        key_id=key_id,
        inviter_connection_prekey_public_key=inviter_connection_prekey_public_key,
        inviter_connection_prekey_shared_id=inviter_connection_prekey_shared_id,
        inviter_connection_prekey_id=inviter_connection_prekey_id,
        inviter_ip=inviter_ip,
        inviter_port=inviter_port,
        link_user_id=link_user_id,
        inviter_peer_shared_blob_id=inviter_peer_shared_blob_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_INVITE_ACCEPTED,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_invite_accepted_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_INVITE_ACCEPTED:
        raise ValueError("unexpected event type for invite_accepted")
    plaintext = payload[:INVITE_ACCEPTED_PLAINTEXT_SIZE]
    decoded = decode_invite_accepted_plaintext(plaintext)
    invite_link_data: dict[str, Any] = {
        "invite_id": crypto.b64encode(decoded["invite_id"]),
        "invite_private_key": crypto.b64encode(decoded["invite_private_key"]),
    }
    if decoded["invite_prekey_id"]:
        invite_link_data["invite_prekey_id"] = crypto.b64encode(decoded["invite_prekey_id"])
    if decoded["inviter_peer_shared_id"]:
        invite_link_data["inviter_peer_shared_id"] = crypto.b64encode(decoded["inviter_peer_shared_id"])
    if decoded["network_id"]:
        invite_link_data["network_id"] = crypto.b64encode(decoded["network_id"])
    if decoded["channel_id"]:
        invite_link_data["channel_id"] = crypto.b64encode(decoded["channel_id"])
    if decoded["key_id"]:
        invite_link_data["key_id"] = crypto.b64encode(decoded["key_id"])
    if decoded["inviter_connection_prekey_public_key"]:
        invite_link_data["inviter_connection_prekey_public_key"] = crypto.b64encode(
            decoded["inviter_connection_prekey_public_key"]
        )
    if decoded["inviter_connection_prekey_shared_id"]:
        invite_link_data["inviter_connection_prekey_shared_id"] = crypto.b64encode(
            decoded["inviter_connection_prekey_shared_id"]
        )
    if decoded["inviter_connection_prekey_id"]:
        invite_link_data["inviter_connection_prekey_id"] = crypto.b64encode(
            decoded["inviter_connection_prekey_id"]
        )
    if decoded["inviter_ip"] is not None:
        invite_link_data["ip"] = decoded["inviter_ip"]
    if decoded["inviter_port"] is not None:
        invite_link_data["port"] = decoded["inviter_port"]
    if decoded["link_user_id"]:
        invite_link_data["user_id"] = crypto.b64encode(decoded["link_user_id"])
    if decoded["inviter_peer_shared_blob_id"]:
        invite_link_data["inviter_peer_shared_blob_id"] = crypto.b64encode(
            decoded["inviter_peer_shared_blob_id"]
        )

    event_data = {
        "type": "invite_accepted",
        "invite_link_data": invite_link_data,
        "created_at": header.created_at_ms,
    }
    if header.signer_id != b"\x00" * SIGNER_ID_SIZE:
        event_data["signed_by"] = crypto.b64encode(header.signer_id)
    return event_data


def encode_self_address_wire_event(
    *,
    peer_id_b64: str,
    ip: str,
    port: int,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    peer_id = crypto.b64decode(peer_id_b64)
    plaintext = encode_self_address_plaintext(peer_id=peer_id, ip=ip, port=port)
    header = WireHeader(
        version=1,
        event_type=TYPE_SELF_ADDRESS,
        flags=0,
        signer_type=SIGNER_PEER_SHARED,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", peer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_self_address_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_SELF_ADDRESS:
        raise ValueError("unexpected event type for self_address")
    plaintext = payload[:SELF_ADDRESS_PLAINTEXT_SIZE]
    decoded = decode_self_address_plaintext(plaintext)
    if decoded["peer_id"] != header.signer_id:
        raise ValueError("peer_id does not match signer_id")
    return {
        "type": "self_address",
        "peer_id": crypto.b64encode(decoded["peer_id"]),
        "ip": decoded["ip"],
        "port": decoded["port"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


def encode_observed_address_wire_event(
    *,
    observed_peer_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    ip: str,
    port: int,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    observed_peer_id = crypto.b64decode(observed_peer_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_observed_address_plaintext(
        observed_peer_id=observed_peer_id,
        ip=ip,
        port=port,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_OBSERVED_ADDRESS,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_observed_address_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_OBSERVED_ADDRESS:
        raise ValueError("unexpected event type for observed_address")
    plaintext = payload[:OBSERVED_ADDRESS_PLAINTEXT_SIZE]
    decoded = decode_observed_address_plaintext(plaintext)
    return {
        "type": "observed_address",
        "observed_peer_id": crypto.b64encode(decoded["observed_peer_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "ip": decoded["ip"],
        "port": decoded["port"],
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


def encode_network_intro_wire_event(
    *,
    peer1_id_b64: str,
    peer2_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    peer1_id = crypto.b64decode(peer1_id_b64)
    peer2_id = crypto.b64decode(peer2_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_network_intro_plaintext(peer1_id=peer1_id, peer2_id=peer2_id)
    header = WireHeader(
        version=1,
        event_type=TYPE_NETWORK_INTRO,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_network_intro_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_NETWORK_INTRO:
        raise ValueError("unexpected event type for network_intro")
    plaintext = payload[:NETWORK_INTRO_PLAINTEXT_SIZE]
    decoded = decode_network_intro_plaintext(plaintext)
    return {
        "type": "network_intro",
        "peer1_id": crypto.b64encode(decoded["peer1_id"]),
        "peer2_id": crypto.b64encode(decoded["peer2_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


def _encode_negentropy_range_id(range_id: str | None) -> bytes:
    if not range_id:
        return b"\x00" * NEGENTROPY_RANGE_ID_SIZE
    raw = bytes.fromhex(range_id)
    return _require_len("range_id", raw, NEGENTROPY_RANGE_ID_SIZE)


def _decode_negentropy_range_id(data: bytes) -> str:
    _require_len("range_id", data, NEGENTROPY_RANGE_ID_SIZE)
    return data.hex()


def _encode_negentropy_prefix(prefix: str | None) -> bytes:
    if not prefix:
        return b""
    if len(prefix) % 2 != 0:
        raise ValueError("prefix must be even-length hex")
    raw = bytes.fromhex(prefix)
    if len(raw) > NEGENTROPY_PREFIX_BYTES:
        raise ValueError("prefix too long")
    return raw


def _decode_negentropy_prefix(prefix_bytes: bytes) -> str:
    return prefix_bytes.hex()


def _encode_negentropy_hash(hex_value: str | None) -> bytes:
    if not hex_value:
        return b"\x00" * 16
    raw = bytes.fromhex(hex_value)
    return _require_len("hash", raw, 16)


def _decode_negentropy_hash(data: bytes) -> str | None:
    _require_len("hash", data, 16)
    if data == b"\x00" * 16:
        return None
    return data.hex()


def encode_negentropy_wire_event(
    *,
    connection_id_b64: str,
    reply_connection_id_b64: str,
    msg: dict[str, Any],
    created_at_ms: int,
) -> bytes:
    msg_type = msg.get("type")
    if msg_type == "range_request":
        msg_type_id = NEGENTROPY_MSG_RANGE_REQUEST
    elif msg_type == "range_matched":
        msg_type_id = NEGENTROPY_MSG_RANGE_MATCHED
    elif msg_type == "range_events":
        msg_type_id = NEGENTROPY_MSG_RANGE_EVENTS
    else:
        raise ValueError("unknown negentropy msg type")

    level_name = msg.get("level")
    if level_name == "root":
        level_id = NEGENTROPY_LEVEL_ROOT
    elif level_name == "prefix_2":
        level_id = NEGENTROPY_LEVEL_PREFIX_2
    elif level_name == "prefix_4":
        level_id = NEGENTROPY_LEVEL_PREFIX_4
    elif level_name == "prefix_6":
        level_id = NEGENTROPY_LEVEL_PREFIX_6
    else:
        level_id = NEGENTROPY_LEVEL_ROOT

    range_id = _encode_negentropy_range_id(msg.get("range_id"))
    prefix_bytes = _encode_negentropy_prefix(msg.get("prefix"))

    if msg_type_id == NEGENTROPY_MSG_RANGE_EVENTS:
        hash_bytes = _encode_negentropy_hash(msg.get("our_hash"))
    else:
        hash_bytes = _encode_negentropy_hash(msg.get("hash"))

    root_hash = _encode_negentropy_hash(msg.get("root_hash"))
    total_events = int(msg.get("total_events") or 0)
    parent_range_id = _encode_negentropy_range_id(msg.get("parent_range_id"))

    event_ids: list[bytes] = []
    if msg_type_id == NEGENTROPY_MSG_RANGE_EVENTS:
        for event_id_b64 in msg.get("event_ids", []):
            event_ids.append(_require_len("event_id", crypto.b64decode(event_id_b64), 16))

    plaintext = encode_negentropy_plaintext(
        connection_id=crypto.b64decode(connection_id_b64),
        reply_connection_id=crypto.b64decode(reply_connection_id_b64),
        msg_type=msg_type_id,
        range_id=range_id,
        level=level_id,
        prefix_bytes=prefix_bytes,
        hash_bytes=hash_bytes,
        root_hash=root_hash,
        total_events=total_events,
        parent_range_id=parent_range_id,
        event_ids=event_ids,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_NEGENTROPY,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * SIGNER_ID_SIZE,
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_negentropy_wire_event(data: bytes) -> dict[str, Any]:
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_NEGENTROPY:
        raise ValueError("unexpected event type for negentropy")
    plaintext = payload[:NEGENTROPY_PLAINTEXT_SIZE]
    decoded = decode_negentropy_plaintext(plaintext)

    msg_type = decoded["msg_type"]
    if msg_type == NEGENTROPY_MSG_RANGE_REQUEST:
        msg_type_str = "range_request"
    elif msg_type == NEGENTROPY_MSG_RANGE_MATCHED:
        msg_type_str = "range_matched"
    elif msg_type == NEGENTROPY_MSG_RANGE_EVENTS:
        msg_type_str = "range_events"
    else:
        raise ValueError("invalid negentropy msg_type")

    level_value = decoded["level"]
    if level_value == NEGENTROPY_LEVEL_ROOT:
        level_str = "root"
    elif level_value == NEGENTROPY_LEVEL_PREFIX_2:
        level_str = "prefix_2"
    elif level_value == NEGENTROPY_LEVEL_PREFIX_4:
        level_str = "prefix_4"
    elif level_value == NEGENTROPY_LEVEL_PREFIX_6:
        level_str = "prefix_6"
    else:
        raise ValueError("invalid negentropy level")

    msg: dict[str, Any] = {
        "type": msg_type_str,
        "range_id": _decode_negentropy_range_id(decoded["range_id"]),
        "root_hash": _decode_negentropy_hash(decoded["root_hash"]) or "",
        "total_events": decoded["total_events"],
    }
    prefix_str = _decode_negentropy_prefix(decoded["prefix_bytes"])
    if prefix_str:
        msg["prefix"] = prefix_str
    if msg_type_str == "range_request":
        msg["level"] = level_str
        msg["prefix"] = prefix_str
        msg["hash"] = _decode_negentropy_hash(decoded["hash_bytes"]) or ""
        parent_range = _decode_negentropy_range_id(decoded["parent_range_id"])
        if parent_range != "0000000000000000":
            msg["parent_range_id"] = parent_range
    elif msg_type_str == "range_events":
        msg["our_hash"] = _decode_negentropy_hash(decoded["hash_bytes"]) or ""
        msg["event_ids"] = [crypto.b64encode(event_id) for event_id in decoded["event_ids"]]
        msg["prefix"] = prefix_str
    else:
        pass

    event_data = {
        "type": "negentropy",
        "connection_id": crypto.b64encode(decoded["connection_id"]),
        "reply_connection_id": crypto.b64encode(decoded["reply_connection_id"]),
        "data": msg,
        "created_at": header.created_at_ms,
    }
    return event_data


# =============================================================================
# TreeKEM Phase 1: is_wire_* helper functions
# =============================================================================

def is_wire_pubkey_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_PUBKEY


def is_wire_secret_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_SECRET


def is_wire_secret_shared_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_SECRET_SHARED


def is_wire_removal_epoch_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_REMOVAL_EPOCH


def is_wire_key_request_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_KEY_REQUEST


# =============================================================================
# TreeKEM Phase 2: is_wire_* helper functions
# =============================================================================

def is_wire_treekem_secret_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_TREEKEM_SECRET


def is_wire_treekem_pubkey_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_TREEKEM_PUBKEY


def is_wire_treekem_update_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_TREEKEM_UPDATE


def is_wire_treekem_secret_shared_envelope(data: bytes) -> bool:
    if len(data) != WIRE_SIZE:
        return False
    try:
        header = WireHeader.unpack(data[:HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == TYPE_TREEKEM_SECRET_SHARED


# =============================================================================
# TreeKEM Phase 1: Pubkey (signed, shareable)
# =============================================================================

def encode_pubkey_plaintext(*, public_key: bytes, removal_epoch_id: bytes | None) -> bytes:
    """Encode pubkey plaintext: public_key (32) + removal_epoch_id (16, nullable)."""
    public_key = _require_len("public_key", public_key, 32)
    removal_epoch_id = removal_epoch_id or b"\x00" * 16
    removal_epoch_id = _require_len("removal_epoch_id", removal_epoch_id, 16)
    return public_key + removal_epoch_id


def decode_pubkey_plaintext(data: bytes) -> dict[str, Any]:
    """Decode pubkey plaintext."""
    public_key = data[:32]
    removal_epoch_id = data[32:48]
    return {
        "public_key": public_key,
        "removal_epoch_id": removal_epoch_id if removal_epoch_id != b"\x00" * 16 else None,
    }


def encode_pubkey_wire_event(
    *,
    public_key: bytes,
    removal_epoch_id_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a signed pubkey wire event."""
    removal_epoch_id = crypto.b64decode(removal_epoch_id_b64) if removal_epoch_id_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_pubkey_plaintext(public_key=public_key, removal_epoch_id=removal_epoch_id)
    header = WireHeader(
        version=1,
        event_type=TYPE_PUBKEY,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_pubkey_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a pubkey wire event."""
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_PUBKEY:
        raise ValueError("unexpected event type for pubkey")
    plaintext = payload[:PUBKEY_PLAINTEXT_SIZE]
    decoded = decode_pubkey_plaintext(plaintext)
    return {
        "type": "pubkey",
        "public_key": crypto.b64encode(decoded["public_key"]),
        "removal_epoch_id": crypto.b64encode(decoded["removal_epoch_id"]) if decoded["removal_epoch_id"] else None,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


# =============================================================================
# TreeKEM Phase 1: Secret (deterministic, local-only)
# =============================================================================

def encode_secret_plaintext(*, key: bytes) -> bytes:
    """Encode secret plaintext: key (32)."""
    return _require_len("key", key, 32)


def decode_secret_plaintext(data: bytes) -> dict[str, Any]:
    """Decode secret plaintext."""
    return {"key": data[:32]}


def encode_secret_wire_event(*, key: bytes, created_at_ms: int) -> bytes:
    """Encode a deterministic secret wire event."""
    plaintext = encode_secret_plaintext(key=key)
    header = WireHeader(
        version=1,
        event_type=TYPE_SECRET,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * SIGNER_ID_SIZE,
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_secret_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a secret wire event."""
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_SECRET:
        raise ValueError("unexpected event type for secret")
    plaintext = payload[:SECRET_PLAINTEXT_SIZE]
    decoded = decode_secret_plaintext(plaintext)
    return {
        "type": "secret",
        "key": crypto.b64encode(decoded["key"]),
        "created_at": header.created_at_ms,
    }


# =============================================================================
# TreeKEM Phase 1: Secret Shared (encrypted to recipient pubkey)
# =============================================================================

def encode_secret_shared_plaintext(
    *,
    secret_id: bytes,
    symmetric_key: bytes,
    recipient_pubkey_id: bytes,
) -> bytes:
    """Encode secret_shared plaintext."""
    secret_id = _require_len("secret_id", secret_id, 16)
    symmetric_key = _require_len("symmetric_key", symmetric_key, 32)
    recipient_pubkey_id = _require_len("recipient_pubkey_id", recipient_pubkey_id, 16)
    return secret_id + symmetric_key + recipient_pubkey_id


def decode_secret_shared_plaintext(data: bytes) -> dict[str, Any]:
    """Decode secret_shared plaintext."""
    return {
        "secret_id": data[:16],
        "symmetric_key": data[16:48],
        "recipient_pubkey_id": data[48:64],
    }


def _encrypt_secret_shared_payload(plaintext: bytes, recipient_pubkey: dict[str, Any]) -> bytes:
    """Encrypt secret_shared payload to recipient's pubkey."""
    public_key = recipient_pubkey["public_key"]
    key_id = recipient_pubkey["id"]
    encrypted = crypto.seal(plaintext, public_key)
    result = key_id + encrypted
    # Pad to PAYLOAD_SIZE
    if len(result) < PAYLOAD_SIZE:
        result = result + b"\x00" * (PAYLOAD_SIZE - len(result))
    return result


def _decrypt_secret_shared_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt secret_shared payload."""
    # Find the actual encrypted length by removing padding
    # sealed_box overhead is 48 bytes (32 ephemeral pubkey + 16 tag)
    encrypted = payload[16:]
    private_key = key_data["private_key"]
    return crypto.unseal(encrypted, private_key)


def encode_secret_shared_wire_event(
    *,
    secret_id_b64: str,
    symmetric_key_b64: str,
    recipient_pubkey_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    recipient_pubkey: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode an encrypted secret_shared wire event."""
    secret_id = crypto.b64decode(secret_id_b64)
    symmetric_key = crypto.b64decode(symmetric_key_b64)
    recipient_pubkey_id = crypto.b64decode(recipient_pubkey_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_secret_shared_plaintext(
        secret_id=secret_id,
        symmetric_key=symmetric_key,
        recipient_pubkey_id=recipient_pubkey_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_SECRET_SHARED,
        flags=FLAG_ENCRYPTED | FLAG_WRAP_ASYM,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_secret_shared_payload(plaintext, recipient_pubkey)
    return build_envelope(header, payload, signature)


def decode_secret_shared_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode an encrypted secret_shared wire event."""
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_SECRET_SHARED:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = None
        if key_cache:
            key_data = key_cache.get(key_id_b64)
        if not key_data:
            key_data = crypto.get_event_key_by_id(key_id, recorded_by, db)
        if not key_data:
            return None, [key_id_b64]
        try:
            plaintext = _decrypt_secret_shared_payload(payload, key_data)
        except Exception:
            return None, [key_id_b64]
    else:
        plaintext = payload[:SECRET_SHARED_PLAINTEXT_SIZE]
    decoded = decode_secret_shared_plaintext(plaintext)
    return {
        "type": "secret_shared",
        "secret_id": crypto.b64encode(decoded["secret_id"]),
        "symmetric_key": crypto.b64encode(decoded["symmetric_key"]),
        "recipient_pubkey_id": crypto.b64encode(decoded["recipient_pubkey_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }, []


# =============================================================================
# TreeKEM Phase 1: Removal Epoch (signed, shareable)
# =============================================================================

def encode_removal_epoch_plaintext(
    *,
    removed_peer_id: bytes,
    removed_user_id: bytes | None,
    parent_epoch_id: bytes | None,
) -> bytes:
    """Encode removal_epoch plaintext."""
    removed_peer_id = _require_len("removed_peer_id", removed_peer_id, 16)
    removed_user_id = removed_user_id or b"\x00" * 16
    removed_user_id = _require_len("removed_user_id", removed_user_id, 16)
    parent_epoch_id = parent_epoch_id or b"\x00" * 16
    parent_epoch_id = _require_len("parent_epoch_id", parent_epoch_id, 16)
    return removed_peer_id + removed_user_id + parent_epoch_id


def decode_removal_epoch_plaintext(data: bytes) -> dict[str, Any]:
    """Decode removal_epoch plaintext."""
    removed_peer_id = data[:16]
    removed_user_id = data[16:32]
    parent_epoch_id = data[32:48]
    return {
        "removed_peer_id": removed_peer_id,
        "removed_user_id": removed_user_id if removed_user_id != b"\x00" * 16 else None,
        "parent_epoch_id": parent_epoch_id if parent_epoch_id != b"\x00" * 16 else None,
    }


def encode_removal_epoch_wire_event(
    *,
    removed_peer_id_b64: str,
    removed_user_id_b64: str | None,
    parent_epoch_id_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a signed removal_epoch wire event."""
    removed_peer_id = crypto.b64decode(removed_peer_id_b64)
    removed_user_id = crypto.b64decode(removed_user_id_b64) if removed_user_id_b64 else None
    parent_epoch_id = crypto.b64decode(parent_epoch_id_b64) if parent_epoch_id_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_removal_epoch_plaintext(
        removed_peer_id=removed_peer_id,
        removed_user_id=removed_user_id,
        parent_epoch_id=parent_epoch_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_REMOVAL_EPOCH,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_removal_epoch_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a removal_epoch wire event."""
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_REMOVAL_EPOCH:
        raise ValueError("unexpected event type for removal_epoch")
    plaintext = payload[:REMOVAL_EPOCH_PLAINTEXT_SIZE]
    decoded = decode_removal_epoch_plaintext(plaintext)
    return {
        "type": "removal_epoch",
        "removed_peer_id": crypto.b64encode(decoded["removed_peer_id"]),
        "removed_user_id": crypto.b64encode(decoded["removed_user_id"]) if decoded["removed_user_id"] else None,
        "parent_epoch_id": crypto.b64encode(decoded["parent_epoch_id"]) if decoded["parent_epoch_id"] else None,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


# =============================================================================
# TreeKEM Phase 1: Key Request (signed, shareable)
# =============================================================================

def encode_key_request_plaintext(
    *,
    requested_key_id: bytes,
    requester_pubkey_id: bytes,
) -> bytes:
    """Encode key_request plaintext."""
    requested_key_id = _require_len("requested_key_id", requested_key_id, 16)
    requester_pubkey_id = _require_len("requester_pubkey_id", requester_pubkey_id, 16)
    return requested_key_id + requester_pubkey_id


def decode_key_request_plaintext(data: bytes) -> dict[str, Any]:
    """Decode key_request plaintext."""
    return {
        "requested_key_id": data[:16],
        "requester_pubkey_id": data[16:32],
    }


def encode_key_request_wire_event(
    *,
    requested_key_id_b64: str,
    requester_pubkey_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a signed key_request wire event."""
    requested_key_id = crypto.b64decode(requested_key_id_b64)
    requester_pubkey_id = crypto.b64decode(requester_pubkey_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_key_request_plaintext(
        requested_key_id=requested_key_id,
        requester_pubkey_id=requester_pubkey_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_KEY_REQUEST,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_key_request_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a key_request wire event."""
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_KEY_REQUEST:
        raise ValueError("unexpected event type for key_request")
    plaintext = payload[:KEY_REQUEST_PLAINTEXT_SIZE]
    decoded = decode_key_request_plaintext(plaintext)
    return {
        "type": "key_request",
        "requested_key_id": crypto.b64encode(decoded["requested_key_id"]),
        "requester_pubkey_id": crypto.b64encode(decoded["requester_pubkey_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


# =============================================================================
# TreeKEM Phase 2: treekem_secret (deterministic, local-only)
# =============================================================================

def encode_treekem_secret_plaintext(*, depth: int, path_prefix: bytes, key: bytes) -> bytes:
    """Encode treekem_secret plaintext: depth (1) + path_prefix (16) + key (32)."""
    if depth < 0 or depth > 255:
        raise ValueError("depth must be 0-255")
    path_prefix_padded = (path_prefix + b"\x00" * 16)[:16]
    key = _require_len("key", key, 32)
    return bytes([depth]) + path_prefix_padded + key


def decode_treekem_secret_plaintext(data: bytes) -> dict[str, Any]:
    """Decode treekem_secret plaintext."""
    depth = data[0]
    path_prefix = data[1:17]
    key = data[17:49]
    return {"depth": depth, "path_prefix": path_prefix, "key": key}


def encode_treekem_secret_wire_event(
    *,
    depth: int,
    path_prefix: bytes,
    key: bytes,
    created_at_ms: int,
) -> bytes:
    """Encode a deterministic treekem_secret wire event."""
    plaintext = encode_treekem_secret_plaintext(depth=depth, path_prefix=path_prefix, key=key)
    header = WireHeader(
        version=1,
        event_type=TYPE_TREEKEM_SECRET,
        flags=FLAG_UNSIGNED,
        signer_type=SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * SIGNER_ID_SIZE,
    )
    payload = _pad_payload(plaintext)
    signature = b"\x00" * SIGNATURE_SIZE
    return build_envelope(header, payload, signature)


def decode_treekem_secret_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a treekem_secret wire event."""
    header, payload, _signature = parse_envelope(data)
    if header.event_type != TYPE_TREEKEM_SECRET:
        raise ValueError("unexpected event type for treekem_secret")
    plaintext = payload[:TREEKEM_SECRET_PLAINTEXT_SIZE]
    decoded = decode_treekem_secret_plaintext(plaintext)
    return {
        "type": "treekem_secret",
        "depth": decoded["depth"],
        "path_prefix": crypto.b64encode(decoded["path_prefix"]),
        "key": crypto.b64encode(decoded["key"]),
        "created_at": header.created_at_ms,
    }


# =============================================================================
# TreeKEM Phase 2: treekem_pubkey (signed, shareable)
# =============================================================================

def encode_treekem_pubkey_plaintext(
    *,
    depth: int,
    path_prefix: bytes,
    public_key: bytes,
    parent_pubkey_id: bytes | None,
    removal_epoch_id: bytes | None,
) -> bytes:
    """Encode treekem_pubkey plaintext."""
    if depth < 0 or depth > 255:
        raise ValueError("depth must be 0-255")
    path_prefix_padded = (path_prefix + b"\x00" * 16)[:16]
    public_key = _require_len("public_key", public_key, 32)
    parent_pubkey_id = parent_pubkey_id or b"\x00" * 16
    parent_pubkey_id = _require_len("parent_pubkey_id", parent_pubkey_id, 16)
    removal_epoch_id = removal_epoch_id or b"\x00" * 16
    removal_epoch_id = _require_len("removal_epoch_id", removal_epoch_id, 16)
    return bytes([depth]) + path_prefix_padded + public_key + parent_pubkey_id + removal_epoch_id


def decode_treekem_pubkey_plaintext(data: bytes) -> dict[str, Any]:
    """Decode treekem_pubkey plaintext."""
    depth = data[0]
    path_prefix = data[1:17]
    public_key = data[17:49]
    parent_pubkey_id = data[49:65]
    removal_epoch_id = data[65:81]
    return {
        "depth": depth,
        "path_prefix": path_prefix,
        "public_key": public_key,
        "parent_pubkey_id": parent_pubkey_id if parent_pubkey_id != b"\x00" * 16 else None,
        "removal_epoch_id": removal_epoch_id if removal_epoch_id != b"\x00" * 16 else None,
    }


def encode_treekem_pubkey_wire_event(
    *,
    depth: int,
    path_prefix: bytes,
    public_key: bytes,
    parent_pubkey_id_b64: str | None,
    removal_epoch_id_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a signed treekem_pubkey wire event."""
    parent_pubkey_id = crypto.b64decode(parent_pubkey_id_b64) if parent_pubkey_id_b64 else None
    removal_epoch_id = crypto.b64decode(removal_epoch_id_b64) if removal_epoch_id_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_treekem_pubkey_plaintext(
        depth=depth,
        path_prefix=path_prefix,
        public_key=public_key,
        parent_pubkey_id=parent_pubkey_id,
        removal_epoch_id=removal_epoch_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_TREEKEM_PUBKEY,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_treekem_pubkey_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a treekem_pubkey wire event."""
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_TREEKEM_PUBKEY:
        raise ValueError("unexpected event type for treekem_pubkey")
    plaintext = payload[:TREEKEM_PUBKEY_PLAINTEXT_SIZE]
    decoded = decode_treekem_pubkey_plaintext(plaintext)
    return {
        "type": "treekem_pubkey",
        "depth": decoded["depth"],
        "path_prefix": crypto.b64encode(decoded["path_prefix"]),
        "public_key": crypto.b64encode(decoded["public_key"]),
        "parent_pubkey_id": crypto.b64encode(decoded["parent_pubkey_id"]) if decoded["parent_pubkey_id"] else None,
        "removal_epoch_id": crypto.b64encode(decoded["removal_epoch_id"]) if decoded["removal_epoch_id"] else None,
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


# =============================================================================
# TreeKEM Phase 2: treekem_update (signed, shareable)
# =============================================================================

def encode_treekem_update_plaintext(
    *,
    author_peer_id: bytes,
    removal_epoch_id: bytes | None,
    base_update_id: bytes | None,
    root_pubkey_id: bytes,
) -> bytes:
    """Encode treekem_update plaintext."""
    author_peer_id = _require_len("author_peer_id", author_peer_id, 16)
    removal_epoch_id = removal_epoch_id or b"\x00" * 16
    removal_epoch_id = _require_len("removal_epoch_id", removal_epoch_id, 16)
    base_update_id = base_update_id or b"\x00" * 16
    base_update_id = _require_len("base_update_id", base_update_id, 16)
    root_pubkey_id = _require_len("root_pubkey_id", root_pubkey_id, 16)
    return author_peer_id + removal_epoch_id + base_update_id + root_pubkey_id


def decode_treekem_update_plaintext(data: bytes) -> dict[str, Any]:
    """Decode treekem_update plaintext."""
    author_peer_id = data[:16]
    removal_epoch_id = data[16:32]
    base_update_id = data[32:48]
    root_pubkey_id = data[48:64]
    return {
        "author_peer_id": author_peer_id,
        "removal_epoch_id": removal_epoch_id if removal_epoch_id != b"\x00" * 16 else None,
        "base_update_id": base_update_id if base_update_id != b"\x00" * 16 else None,
        "root_pubkey_id": root_pubkey_id,
    }


def encode_treekem_update_wire_event(
    *,
    author_peer_id_b64: str,
    removal_epoch_id_b64: str | None,
    base_update_id_b64: str | None,
    root_pubkey_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a signed treekem_update wire event."""
    author_peer_id = crypto.b64decode(author_peer_id_b64)
    removal_epoch_id = crypto.b64decode(removal_epoch_id_b64) if removal_epoch_id_b64 else None
    base_update_id = crypto.b64decode(base_update_id_b64) if base_update_id_b64 else None
    root_pubkey_id = crypto.b64decode(root_pubkey_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_treekem_update_plaintext(
        author_peer_id=author_peer_id,
        removal_epoch_id=removal_epoch_id,
        base_update_id=base_update_id,
        root_pubkey_id=root_pubkey_id,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_TREEKEM_UPDATE,
        flags=0,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _pad_payload(plaintext)
    return build_envelope(header, payload, signature)


def decode_treekem_update_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a treekem_update wire event."""
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_TREEKEM_UPDATE:
        raise ValueError("unexpected event type for treekem_update")
    plaintext = payload[:TREEKEM_UPDATE_PLAINTEXT_SIZE]
    decoded = decode_treekem_update_plaintext(plaintext)
    return {
        "type": "treekem_update",
        "author_peer_id": crypto.b64encode(decoded["author_peer_id"]),
        "removal_epoch_id": crypto.b64encode(decoded["removal_epoch_id"]) if decoded["removal_epoch_id"] else None,
        "base_update_id": crypto.b64encode(decoded["base_update_id"]) if decoded["base_update_id"] else None,
        "root_pubkey_id": crypto.b64encode(decoded["root_pubkey_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }


# =============================================================================
# TreeKEM Phase 2: treekem_secret_shared (encrypted to recipient pubkey)
# =============================================================================

def encode_treekem_secret_shared_plaintext(
    *,
    treekem_secret_id: bytes,
    symmetric_key: bytes,
    recipient_pubkey_id: bytes,
    source_update_id: bytes | None,
    depth: int,
) -> bytes:
    """Encode treekem_secret_shared plaintext."""
    treekem_secret_id = _require_len("treekem_secret_id", treekem_secret_id, 16)
    symmetric_key = _require_len("symmetric_key", symmetric_key, 32)
    recipient_pubkey_id = _require_len("recipient_pubkey_id", recipient_pubkey_id, 16)
    source_update_id = source_update_id or b"\x00" * 16
    source_update_id = _require_len("source_update_id", source_update_id, 16)
    if depth < 0 or depth > 255:
        raise ValueError("depth must be 0-255")
    return treekem_secret_id + symmetric_key + recipient_pubkey_id + source_update_id + bytes([depth])


def decode_treekem_secret_shared_plaintext(data: bytes) -> dict[str, Any]:
    """Decode treekem_secret_shared plaintext."""
    treekem_secret_id = data[:16]
    symmetric_key = data[16:48]
    recipient_pubkey_id = data[48:64]
    source_update_id = data[64:80]
    depth = data[80] if len(data) > 80 else 0
    return {
        "treekem_secret_id": treekem_secret_id,
        "symmetric_key": symmetric_key,
        "recipient_pubkey_id": recipient_pubkey_id,
        "source_update_id": source_update_id if source_update_id != b"\x00" * 16 else None,
        "depth": depth,
    }


def _encrypt_treekem_secret_shared_payload(plaintext: bytes, recipient_pubkey: dict[str, Any]) -> bytes:
    """Encrypt treekem_secret_shared payload to recipient's pubkey."""
    public_key = recipient_pubkey["public_key"]
    key_id = recipient_pubkey["id"]
    encrypted = crypto.seal(plaintext, public_key)
    result = key_id + encrypted
    # Pad to PAYLOAD_SIZE
    if len(result) < PAYLOAD_SIZE:
        result = result + b"\x00" * (PAYLOAD_SIZE - len(result))
    return result


def _decrypt_treekem_secret_shared_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt treekem_secret_shared payload."""
    encrypted = payload[16:]
    private_key = key_data["private_key"]
    return crypto.unseal(encrypted, private_key)


def encode_treekem_secret_shared_wire_event(
    *,
    treekem_secret_id_b64: str,
    symmetric_key_b64: str,
    recipient_pubkey_id_b64: str,
    source_update_id_b64: str | None,
    depth: int,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    recipient_pubkey: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode an encrypted treekem_secret_shared wire event."""
    treekem_secret_id = crypto.b64decode(treekem_secret_id_b64)
    symmetric_key = crypto.b64decode(symmetric_key_b64)
    recipient_pubkey_id = crypto.b64decode(recipient_pubkey_id_b64)
    source_update_id = crypto.b64decode(source_update_id_b64) if source_update_id_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_treekem_secret_shared_plaintext(
        treekem_secret_id=treekem_secret_id,
        symmetric_key=symmetric_key,
        recipient_pubkey_id=recipient_pubkey_id,
        source_update_id=source_update_id,
        depth=depth,
    )
    header = WireHeader(
        version=1,
        event_type=TYPE_TREEKEM_SECRET_SHARED,
        flags=FLAG_ENCRYPTED | FLAG_WRAP_ASYM,
        signer_type=signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=_require_len("signer_id", signer_id, SIGNER_ID_SIZE),
    )
    signed_bytes = _signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_treekem_secret_shared_payload(plaintext, recipient_pubkey)
    return build_envelope(header, payload, signature)


def decode_treekem_secret_shared_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode an encrypted treekem_secret_shared wire event."""
    header, payload, signature = parse_envelope(data)
    if header.event_type != TYPE_TREEKEM_SECRET_SHARED:
        return None, []
    if header.flags & FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = None
        if key_cache:
            key_data = key_cache.get(key_id_b64)
        if not key_data:
            key_data = crypto.get_event_key_by_id(key_id, recorded_by, db)
        if not key_data:
            return None, [key_id_b64]
        try:
            plaintext = _decrypt_treekem_secret_shared_payload(payload, key_data)
        except Exception:
            return None, [key_id_b64]
    else:
        plaintext = payload[:TREEKEM_SECRET_SHARED_PLAINTEXT_SIZE]
    decoded = decode_treekem_secret_shared_plaintext(plaintext)
    return {
        "type": "treekem_secret_shared",
        "treekem_secret_id": crypto.b64encode(decoded["treekem_secret_id"]),
        "symmetric_key": crypto.b64encode(decoded["symmetric_key"]),
        "recipient_pubkey_id": crypto.b64encode(decoded["recipient_pubkey_id"]),
        "source_update_id": crypto.b64encode(decoded["source_update_id"]) if decoded["source_update_id"] else None,
        "depth": decoded["depth"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": _signing_bytes(header, plaintext),
    }, []
