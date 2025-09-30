"""
Helpers for per-identity seen gating using local-only seen_local events in ES.
"""
from __future__ import annotations

from typing import Optional
from .db import ReadOnlyConnection
import hashlib


def compute_seen_local_id(identity_id: str, event_id: str) -> str:
    """Compute deterministic seen_local event_id = blake2b16(peer_id || 0x00 || event_id)."""
    pid = _try_hex_bytes(identity_id)
    eid = event_id.encode('utf-8')
    digest = hashlib.blake2b(pid + b"\x00" + eid, digest_size=16).hexdigest()
    return digest


def has_seen(db: ReadOnlyConnection, identity_id: str, event_id: str) -> bool:
    """Return True if ES contains the local-only seen_local for this (identity,event)."""
    sid = compute_seen_local_id(identity_id, event_id)
    row = db.execute(
        "SELECT 1 FROM events WHERE event_id = ? AND visibility = 'local-only' LIMIT 1",
        (sid,),
    ).fetchone()
    return row is not None


def _try_hex_bytes(value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except Exception:
        return value.encode('utf-8')

