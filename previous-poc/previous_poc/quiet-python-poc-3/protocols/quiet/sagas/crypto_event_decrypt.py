from __future__ import annotations

"""Event-layer decrypt saga (scaffold).

Consumes event.wire; for types marked encrypted in the registry, emits
event.encrypted and, if CRYPTO_MODE=dummy, immediately event.decrypted
by treating the body as plaintext.
"""

from typing import Any, Dict, List
import os

from protocols.quiet.event_types.registry import is_encrypted

SAGA_NAME = "quiet.crypto.event_decrypt"
SUBSCRIBE = {"types": ["event.wire"]}


def process(events: List[Dict[str, Any]], db: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    dummy = os.environ.get("CRYPTO_MODE", "real") == "dummy"

    for ev in events:
        data = ev.get("data", {})
        raw: bytes = data.get("raw", b"")
        # Minimal header sniffing: assume first byte is type_code in dummy mode
        type_code = raw[1] if len(raw) > 1 else 0x00
        enc = is_encrypted(type_code)
        wid = data.get("wire_id")

        if enc:
            out.append({
                "type": "event.encrypted",
                "data": {
                    "wire_id": wid,
                    "type_code": type_code,
                    "cipher": raw,
                },
            })
            if dummy:
                # In dummy mode, pretend the body is plaintext
                out.append({
                    "type": "event.decrypted",
                    "data": {
                        "id": wid,
                        "type_code": type_code,
                        "payload": {"raw": raw.hex()},
                    },
                })
        else:
            # Treat non-encrypted types as already-decrypted
            out.append({
                "type": "event.decrypted",
                "data": {
                    "id": wid,
                    "type_code": type_code,
                    "payload": {"raw": raw.hex()},
                },
            })

    return out

