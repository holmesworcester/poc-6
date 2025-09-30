from __future__ import annotations

"""Event type registry for the Quiet protocol.

Provides a single source of truth for:
- Wire codes (0xNN) ↔ human-readable names (e.g., "message").
- Whether a type is event-layer encrypted.
- One-hop dependency fields (DEPS) used by the framework to hydrate context.

This allows processors to rely on framework-provided hydration and
block/unblock behavior for referenced IDs (group_id, channel_id, key_id, etc.).
"""

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional


@dataclass(frozen=True)
class EventSpec:
    name: str
    code: int  # 0xNN as int
    encrypted: bool
    deps: List[str]


# Subset of Appendix A; expand as needed.
_SPECS: List[EventSpec] = [
    EventSpec("message", 0x00, True, ["channel_id", "key_id"]),
    EventSpec("channel", 0x01, True, ["group_id", "key_id"]),
    EventSpec("update", 0x02, True, ["event_id", "user_id", "key_id"]),
    EventSpec("slice", 0x03, False, ["blob_id"]),
    EventSpec("rekey", 0x04, True, ["original_event_id", "new_key_id"]),
    EventSpec("delete-message", 0x05, True, ["message_id", "key_id"]),
    EventSpec("delete-channel", 0x06, True, ["channel_id", "key_id"]),
    EventSpec("sync", 0x07, False, []),
    EventSpec("sync-auth", 0x08, False, []),
    EventSpec("sync-lazy", 0x09, False, ["channel_id"]),
    EventSpec("sync-blob", 0x0A, False, ["blob_id"]),
    EventSpec("intro", 0x0B, False, ["address1_id", "address2_id"]),
    EventSpec("address", 0x0C, False, []),
    EventSpec("invite", 0x0D, False, ["network_id"]),
    EventSpec("user", 0x0E, False, ["network_id"]),
    EventSpec("link-invite", 0x0F, False, ["user_id", "network_id"]),
    EventSpec("link", 0x10, False, ["user_id", "network_id"]),
    EventSpec("remove-peer", 0x11, False, ["peer_id"]),
    EventSpec("remove-user", 0x12, False, ["user_id"]),
    EventSpec("group", 0x14, True, ["user_id", "key_id"]),
    EventSpec("update-group-name", 0x15, True, ["group_id", "key_id"]),
    EventSpec("fixed-group", 0x16, True, ["key_id"]),
    EventSpec("grant", 0x17, True, ["group_id", "user_id", "key_id"]),
    EventSpec("key", 0x18, False, ["prekey_id"]),
    EventSpec("prekey", 0x19, False, ["group_id", "channel_id"]),
    EventSpec("push-server", 0x1A, True, ["user_id", "key_id"]),
    EventSpec("push-register", 0x1B, True, ["key_id"]),
    EventSpec("push-mute", 0x1C, True, ["channel_id", "key_id"]),
    EventSpec("push-unmute", 0x1D, True, ["channel_id", "key_id"]),
    EventSpec("mute-channel", 0x1E, True, ["channel_id", "key_id"]),
    EventSpec("channel-update", 0x1F, True, ["channel_id", "key_id"]),
    EventSpec("unblock", 0x20, True, ["key_id"]),
    EventSpec("seen", 0x21, True, ["channel_id", "message_id", "key_id"]),
]


_BY_NAME: Dict[str, EventSpec] = {s.name: s for s in _SPECS}
_BY_CODE: Dict[int, EventSpec] = {s.code: s for s in _SPECS}


def spec_for_name(name: str) -> Optional[EventSpec]:
    return _BY_NAME.get(name)


def spec_for_code(code: int) -> Optional[EventSpec]:
    return _BY_CODE.get(code)


def code_for(name: str) -> Optional[int]:
    s = _BY_NAME.get(name)
    return s.code if s else None


def name_for(code: int) -> Optional[str]:
    s = _BY_CODE.get(code)
    return s.name if s else None


def is_encrypted(name_or_code: str | int) -> bool:
    if isinstance(name_or_code, int):
        s = _BY_CODE.get(name_or_code)
    else:
        s = _BY_NAME.get(name_or_code)
    return bool(s and s.encrypted)


def deps_for(name_or_code: str | int) -> List[str]:
    if isinstance(name_or_code, int):
        s = _BY_CODE.get(name_or_code)
    else:
        s = _BY_NAME.get(name_or_code)
    return list(s.deps) if s else []


def key_field(name_or_code: str | int) -> Optional[str]:
    """Return the key-id field name for encrypted types, else None."""
    return "key_id" if is_encrypted(name_or_code) else None


def registry_table() -> Mapping[str, EventSpec]:
    """Expose a read-only name→spec mapping (for debug/UI)."""
    return dict(_BY_NAME)

