from __future__ import annotations

"""Event parser saga (scaffold).

Consumes transit.prekey.opened and emits event.wire for each 512-byte
event contained in the opened payloads. In dummy mode, accepts raw bytes
of any length and hashes for id/metadata placeholders.
"""

from typing import Any, Dict, List
import hashlib

SAGA_NAME = "quiet.event.parse"
SUBSCRIBE = {"types": ["transit.prekey.opened"]}


def _wire_id(blob: bytes) -> str:
    # BLAKE2b-128 placeholder using hashlib.blake2b(digest_size=16)
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def process(events: List[Dict[str, Any]], db: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ev in events:
        payloads = ev.get("data", {}).get("payloads", [])
        for raw in payloads:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            wid = _wire_id(raw)
            out.append({
                "type": "event.wire",
                "data": {
                    "wire_id": wid,
                    "raw": bytes(raw),
                },
            })
    return out

