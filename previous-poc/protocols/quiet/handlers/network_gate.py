"""
Network Gate handler (simple consistency check).

Purpose:
- After decrypt/open and dependency resolution, ensure the attributed
  network from the (resolved) transit key matches the network stated in
  the event plaintext (if present). This prevents cross-network replay or
  misattribution without trusting payload fields to set attribution.

Behavior:
- Reads the attributed network from resolved deps:
  - transit path (DEM): resolved_deps['transit_secret:<id>'].network_id
  - sealed path (KEM): resolved_deps['prekey_secret:<id>'].network_id
- If event_plaintext.network_id is present and differs, annotate an error.
- If envelope.network_id is absent, set it from the resolved dep.

Notes:
- No tables are used; this is a pure consistency check and pass-through.
"""

from __future__ import annotations

from typing import Any, List
from core.handlers import Handler


class NetworkGateHandler(Handler):
    @property
    def name(self) -> str:
        return "network_gate"

    def filter(self, envelope: dict[str, Any]) -> bool:
        # Run only once per envelope with plaintext and resolved deps
        if not isinstance(envelope, dict):
            return False
        if envelope.get('network_checked') is True:
            return False
        if not isinstance(envelope.get('event_plaintext'), dict):
            return False
        # Require resolved_deps to contain either transit_secret:* or prekey_secret:*
        resolved = envelope.get('resolved_deps') or {}
        if not isinstance(resolved, dict):
            return False
        for k in resolved.keys():
            if isinstance(k, str) and (k.startswith('transit_secret:') or k.startswith('prekey_secret:')):
                return True
        return False

    def process(self, envelope: dict[str, Any], db) -> List[dict[str, Any]]:
        resolved = envelope.get('resolved_deps') or {}
        net_from_dep: str | None = None
        if isinstance(resolved, dict):
            for k, v in resolved.items():
                if not isinstance(k, str) or not isinstance(v, dict):
                    continue
                if k.startswith('transit_secret:') or k.startswith('prekey_secret:'):
                    nid = v.get('network_id')
                    if isinstance(nid, str) and nid:
                        net_from_dep = nid
                        break

        if net_from_dep:
            # Attach authoritative network if missing
            if not isinstance(envelope.get('network_id'), str) or not envelope.get('network_id'):
                envelope['network_id'] = net_from_dep
            # Consistency check with plaintext (if present)
            pt = envelope.get('event_plaintext') or {}
            claimed = pt.get('network_id') if isinstance(pt, dict) else None
            if isinstance(claimed, str) and claimed and claimed != net_from_dep:
                # Do not emit content on mismatch
                return []
            # Attach viewer identity from transit/prekey deps when available
            try:
                viewer_psid: str | None = None
                viewer_pid: str | None = None
                # Prefer transit_secret owner
                tkey = next((k for k in resolved.keys() if isinstance(k, str) and k.startswith('transit_secret:')), None)
                if tkey:
                    tv = resolved.get(tkey) or {}
                    if isinstance(tv.get('peer_secret_id'), str) and tv.get('peer_secret_id'):
                        viewer_psid = tv.get('peer_secret_id')
                    if isinstance(tv.get('peer_id'), str) and tv.get('peer_id'):
                        viewer_pid = tv.get('peer_id')
                else:
                    # Fallback to prekey owner if encoded in dep
                    pkey = next((k for k in resolved.keys() if isinstance(k, str) and k.startswith('prekey_secret:')), None)
                    if pkey:
                        pv = resolved.get(pkey) or {}
                        if isinstance(pv.get('peer_secret_id'), str) and pv.get('peer_secret_id'):
                            viewer_psid = pv.get('peer_secret_id')
                        if isinstance(pv.get('peer_id'), str) and pv.get('peer_id'):
                            viewer_pid = pv.get('peer_id')
                # Always set to_peer to the peer_secret_id when available
                if isinstance(viewer_psid, str) and viewer_psid:
                    envelope['to_peer'] = viewer_psid
                # Also expose peer id for completeness
                if isinstance(viewer_pid, str) and viewer_pid:
                    envelope['to_peer_id'] = viewer_pid
            except Exception:
                pass
            envelope['network_checked'] = True
            return [envelope]
