from __future__ import annotations

"""Typed payload stubs for selected event types.

These are intentionally minimal — they establish names, shapes, and
type hints to keep processors readable. Expand as needed.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MessagePayload:
    channel_id: str
    text: str
    # Encrypted header carries key_id separately; not included here.


@dataclass
class ChannelPayload:
    group_id: str
    channel_name: str
    disappearing_time_ms: int


@dataclass
class UpdatePayload:
    event_id: str
    global_count: int
    update_code: int
    user_id: str
    body: bytes  # fixed-size body per Appendix B


@dataclass
class PrekeyPayload:
    group_id: str
    channel_id: str
    prekey_pub: bytes
    eol_ms: int


@dataclass
class KeyPayload:
    peer_pk: bytes
    count: int
    created_ms: int
    ttl_ms: int
    tag_id: str  # acls/scope tag; accompanies key
    prekey_id: str
    sealed_key: bytes


@dataclass
class RekeyPayload:
    original_event_id: str
    new_key_id: str
    new_ciphertext: bytes


@dataclass
class SlicePayload:
    blob_id: str
    slice_no: int
    nonce24: bytes
    ciphertext: bytes
    poly_tag: bytes


@dataclass
class AddressPayload:
    transport: int
    addr: str
    port: int

