"""
Small, pure helpers used by crypto handlers to reduce duplication.

Keep protocol-specific logic minimal and reusable. Do not import DB here.
"""
from __future__ import annotations
from typing import Any, Optional
from core.crypto import hash as blake2b


def as_bytes(val: Any) -> Optional[bytes]:
    """Best-effort conversion to bytes for secrets and ciphertext fields.

    Accepts bytes/bytearray (returned as bytes) or hex-encoded strings.
    Returns None if conversion fails.
    """
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    if isinstance(val, str):
        s = val.strip()
        # Fast-path: even length and hex-like
        if len(s) % 2 == 0:
            try:
                return bytes.fromhex(s)
            except Exception:
                return None
    return None


def deterministic_nonce(key_id: str, pt_bytes: bytes, size: int = 24) -> bytes:
    """Derive a deterministic nonce from key_id and canonical plaintext.

    This keeps ciphertext/event_id stable for identical (key, plaintext).
    """
    return blake2b((key_id + ':').encode('utf-8') + pt_bytes, size=size)


def choose_dep(resolved_deps: dict[str, Any] | None, candidates: list[str]) -> Optional[dict[str, Any]]:
    """Pick the first available dependency record from resolved_deps.

    candidates is a list of dep keys to try in order (e.g., ["key_secret:abc", "key:abc"]).
    Returns the dep dict or None.
    """
    if not resolved_deps:
        return None
    for k in candidates:
        if k in resolved_deps and isinstance(resolved_deps[k], dict):
            return resolved_deps[k]
    return None

