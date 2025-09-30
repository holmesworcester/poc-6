"""
Flow for periodic sync requests.
"""
from __future__ import annotations

import uuid
import time
from typing import Dict, Any, List

from core.flows import FlowCtx, flow_op
from typing import Optional
from core.crypto import generate_secret, hash as blake2b


@flow_op()  # Registers as 'sync_request.run'
def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Emit sync_request events from each identity to other users in their networks.

    Params:
    - since_ms (optional): last sync cutoff (default 0)

    Returns: { ids: {}, data: {sent: N} }
    """
    ctx = FlowCtx.from_params(params)
    since_ms = int(params.get('since_ms', 0))
    # Prekey selection criteria (defaults reasonable for sync)
    min_prekey_ttl_ms = int(params.get('min_prekey_ttl_ms', 60_000))
    prefer_newest = bool(params.get('prefer_newest_prekey', True))

    # Map peers to networks via users (query)
    pairs = ctx.query('user.list_by_network', {}) or []

    sent = 0
    for row in pairs:
        peer_id_sender = row['peer_id'] if isinstance(row, dict) else row[0]
        network_id = row['network_id'] if isinstance(row, dict) else row[1]

        # Targets: other users in this network (query + filter sender)
        rows_in_net = ctx.query('user.list_by_network', {'network_id': network_id}) or []
        targets = [{'target_peer_id': r['peer_id']} for r in rows_in_net if r.get('peer_id') != peer_id_sender]

        now_ms = int(time.time() * 1000)
        for row_t in targets:
            target_peer_id = row_t['target_peer_id'] if isinstance(row_t, dict) else row_t[0]
            # Choose recipient prekey per criteria (best-effort; fallback to identity if none)
            # 1) Store a signed sync_request event (plaintext stored, no event-layer encryption)
            req_id = str(uuid.uuid4())
            ctx.emit_event({
                'event_type': 'sync_request',
                'event_plaintext': {
                    'type': 'sync_request',
                    'request_id': req_id,
                    'network_id': network_id,
                    'created_by': peer_id_sender,
                    'to_peer': target_peer_id,
                    'timestamp_ms': now_ms,
                    'sync_all': True,
                },
                'peer_id': peer_id_sender,
                'deps': [],
                'self_created': True,
                'is_outgoing': False,
            })

            # 2) Send a sealed KEM request to recipient's prekey (transit-only copy)
            prekey_row: Optional[dict] = None
            try:
                prekey_row = ctx.query('prekey.get_active_for_peer', {
                    'peer_id': target_peer_id,
                    'network_id': network_id,
                    'min_expires_ms': int(time.time() * 1000) + min_prekey_ttl_ms,
                    'prefer_newest': prefer_newest,
                })
            except Exception:
                prekey_row = None

            # Resolve destination address for the target via query
            dest_ip, dest_port = '127.0.0.1', 8080
            try:
                latest = ctx.query('address.get_latest_for_peer', {'peer_id': target_peer_id, 'network_id': network_id})
                if isinstance(latest, dict):
                    dest_ip = latest.get('ip', dest_ip)
                    dest_port = latest.get('port', dest_port)
            except Exception:
                pass

            # Generate a per-request transit secret to allow DEM responses.
            # Flows are responsible for including it in the sealed plaintext and
            # also surfacing the id on the envelope for local indexing.
            _tsec = generate_secret()
            _tsid = blake2b(_tsec, size=32).hex()

            payload = {
                'type': 'sync_request',
                'request_id': req_id,
                'network_id': network_id,
                'created_by': peer_id_sender,
                'to_peer': target_peer_id,
                'timestamp_ms': now_ms,
                'sync_all': True,
                'transit_secret': _tsec.hex(),
                'transit_secret_id': _tsid,
            }
            # include prekey_public if available to drive sealing
            if prekey_row and prekey_row.get('public_key'):
                payload['prekey_public'] = prekey_row['public_key']
            if prekey_row and prekey_row.get('prekey_id'):
                payload['prekey_secret_id'] = prekey_row['prekey_id']

            ctx.emit_event({
                'event_type': 'sync_request',
                'event_plaintext': payload,
                'peer_id': peer_id_sender,
                'deps': [],
                'self_created': False,
                'is_outgoing': True,
                'seal_to': target_peer_id,
                # Surface transit id locally to aid network_gate indexing
                'transit_secret_id': _tsid,
                'network_id': network_id,
                'dest_ip': dest_ip,
                'dest_port': dest_port,
            })
            sent += 1

    return {'ids': {}, 'data': {'sent': sent}}
