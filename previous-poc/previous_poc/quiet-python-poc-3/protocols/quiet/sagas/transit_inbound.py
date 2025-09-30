from __future__ import annotations

"""Transit inbound saga (scaffold).

Consumes LOCAL-ONLY incoming datagrams and emits transit.prekey.opened
events after opening prekey-sealed payloads.

This is a stub: actual crypto is not implemented. In CRYPTO_MODE=dummy,
we just wrap bytes as-is and attach an example prekey_id/network_id.
"""

from typing import Any, Dict, List
import os

SAGA_NAME = "quiet.transit.inbound"
SUBSCRIBE = {"types": ["LOCAL-ONLY-incoming"]}


def process(events: List[Dict[str, Any]], db: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    dummy = os.environ.get("CRYPTO_MODE", "real") == "dummy"

    for ev in events:
        pkt = ev.get("data", {})
        # In a real implementation, we'd locate the matching transit prekey
        # and open the payload(s), then attribute to (network_id, peer_remote_pk).
        if dummy:
            out.append(
                {
                    "type": "transit.prekey.opened",
                    "data": {
                        "prekey_id": pkt.get("prekey_id", "00" * 16),
                        "network_id": pkt.get("network_id", "11" * 16),
                        "peer_remote_pk": pkt.get("peer_remote_pk", "22" * 32),
                        "reply_path": pkt.get("origin"),
                        "payloads": pkt.get("payloads", [pkt.get("bytes", b"")]),
                    },
                }
            )
        else:
            # TODO: implement transit prekey open
            pass

    return out

