"""Reflector for sync request - reflects sync requests with events."""

import sqlite3
from typing import Dict, List, Any, Tuple
from core.db import ReadOnlyConnection


def sync_request_reflector(envelope: Dict, db: sqlite3.Connection, time_now_ms: int) -> Tuple[bool, List[Dict]]:
    """
    Simple sync response reflector - reflects sync requests with events.

    When an identity receives a sync_request, it sends back events from that network.
    """
    try:
        # Don't reflect to sync_request events that are already responses
        if envelope.get('in_response_to'):
            print("[sync_request_reflector] Ignoring sync_request that is already a response")
            return True, []

        request = envelope.get('event_plaintext', {})
        network_id = request.get('network_id')
        request_id = request.get('request_id')
        created_by_peer = request.get('created_by')  # Who sent this request (peer)
        to_peer = request.get('to_peer')  # Which of our peers received it
        # last_sync_ms intentionally ignored for now; we send all events and rely on dedupe

        if not network_id:
            print("[sync_request_reflector] No network_id in sync request")
            return False, []

        if not created_by_peer:
            print("[sync_request_reflector] No created_by in sync request")
            return False, []

        # TODO: Move user/network membership check upstream to validate handler
        # For now, we check here but this should be handled by centralized validation
        # The validate handler should verify:
        # 1. to_peer is actually one of our identities
        # 2. to_peer is a member of the requested network
        # 3. from_identity has permission to request sync from us
        # Once validation is centralized, this reflector can assume the request is valid

        # Validate the recipient peer is a user in this network (should be validated already)
        try:
            user_exists = bool(db.execute(
                """
                SELECT 1 FROM users u
                WHERE u.peer_id = ? AND u.network_id = ?
                LIMIT 1
                """,
                (to_peer, network_id),
            ).fetchone())
        except Exception:
            user_exists = False

        if not user_exists:
            print(f"[sync_request_reflector] Identity {to_peer} not found in network {network_id}")
            return True, []  # Not an error, just not for us

        # Get all canonical events (network-visible). Receiver validates/gates on ingest.
        cur = db.execute(
            """
            SELECT e.event_id, e.event_blob AS event_blob
            FROM events e
            WHERE e.visibility = 'network'
            ORDER BY rowid ASC
            """
        )
        events = [{'event_id': r['event_id'], 'event_type': 'unknown', 'event_blob': r['event_blob']} for r in cur.fetchall()]

        # Extract per-request transit secret for symmetric responses (if provided)
        transit_secret_hex = request.get('transit_secret')
        transit_secret_id = request.get('transit_secret_id')

        # Prepare envelopes: persist transit secret locally, then responses
        response_envelopes: list[Dict[str, Any]] = []

        # If request provided a per-request transit secret, persist it as a local key_secret
        # so subsequent processing can reference it (and for future reuse/TTL management).
        if isinstance(transit_secret_hex, str) and isinstance(transit_secret_id, str):
            try:
                now_ms = time_now_ms
                from core.crypto import get_root_secret_id
                root_id = get_root_secret_id()
                response_envelopes.append({
                    'event_type': 'key_secret',
                    'event_plaintext': {
                        'type': 'key_secret',
                        'unsealed_secret': transit_secret_hex,
                        'created_at': now_ms,
                        'created_by': to_peer,
                    },
                    'peer_id': to_peer,
                    'local_only': True,
                    'deps': [root_id],
                    'self_created': True,
                    'key_secret_id': root_id,
                })
            except Exception:
                pass
        # Resolve destination address for the requester (created_by)
        dest_ip = '127.0.0.1'
        dest_port = 8080
        try:
            addr_row = db.execute(
                """
                SELECT a.ip, a.port
                FROM addresses a
                WHERE a.peer_id = ? AND a.is_active = TRUE
                ORDER BY a.registered_at_ms DESC
                LIMIT 1
                """,
                (created_by_peer,),
            ).fetchone()
            if addr_row:
                dest_ip = addr_row['ip']
                dest_port = addr_row['port']
        except Exception:
            pass
        for event in events:
            response = {
                'event_id': event['event_id'],
                'event_type': event['event_type'],
                'event_blob': event['event_blob'],
                'peer_id': to_peer,  # Our sending peer
                # Use symmetric transit encryption per request
                'transit_secret_id': transit_secret_id,
                'outgoing_checked': True,
                'is_outgoing': True,
                'network_id': network_id,
                'in_response_to': request_id,
                'dest_ip': dest_ip,
                'dest_port': dest_port,
                # Important: set the actual recipient peer so transit hints can carry it
                'to_peer': created_by_peer,
            }
            # Provide resolved transit secret directly to crypto handler to avoid local storage dependency
            if transit_secret_hex and transit_secret_id:
                response['deps_included_and_valid'] = True
                response['resolved_deps'] = {
                    f'transit_secret:{transit_secret_id}': {
                        'transit_secret': bytes.fromhex(transit_secret_hex) if isinstance(transit_secret_hex, str) else transit_secret_hex,
                        'network_id': network_id,
                    }
                }
            response_envelopes.append(response)

        print(f"[sync_request_reflector] Peer {to_peer} sending {len(response_envelopes)} events to {created_by_peer}")
        return True, response_envelopes

    except Exception as e:
        print(f"[sync_request_reflector] Error: {e}")
        return False, []
