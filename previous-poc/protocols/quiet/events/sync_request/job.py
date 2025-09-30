"""Job for sync request - periodically syncs with peers."""

import sqlite3
import uuid
from typing import Dict, List, Any, Tuple
from core.crypto import generate_secret, hash as blake2b


def sync_request_job(state: Dict, db: sqlite3.Connection, time_now_ms: int) -> Tuple[bool, Dict, List[Dict]]:
    """
    Simple sync request job - sends sync requests from each identity to its peers.

    State tracks:
    - last_sync_ms: Last time we ran sync
    """
    try:
        # Initialize state if needed
        if not state:
            state = {'last_sync_ms': 0}

        # Get all identities and their associated networks through the users table
        # Only sync for identities where we have the private key (peer_locals)
        cursor = db.cursor()
        identity_networks = cursor.execute("""
            SELECT DISTINCT u.user_id as identity_id, u.network_id
            FROM users u
            INNER JOIN peer_locals pl ON u.user_id = pl.peer_id
            WHERE u.network_id != ''
        """).fetchall()

        if not identity_networks:
            print(f"[sync_request_job] No identities with network associations found")
            return True, state, []

        envelopes = []

        # For each identity, sync with peers in their networks
        for identity_id, network_id in identity_networks:
            # Get all other users in this network (potential sync targets)
            peers = cursor.execute("""
                SELECT DISTINCT user_id
                FROM users
                WHERE network_id = ? AND user_id != ?
            """, (network_id, identity_id)).fetchall()

            # Create sync request for each peer
            for (peer_id,) in peers:
                # Generate a per-request transit secret so the recipient can respond via DEM
                tsec = generate_secret()
                tsid = blake2b(tsec, size=32).hex()

                envelope = {
                    'event_type': 'sync_request',
                    'event_plaintext': {
                        'request_id': str(uuid.uuid4()),
                        'network_id': network_id,
                        'from_identity': identity_id,
                        'to_peer': peer_id,
                        'timestamp_ms': time_now_ms,
                        'last_sync_ms': state['last_sync_ms'],
                        # Simple approach: just request everything since last sync
                        'sync_all': True,
                        'transit_secret': tsec.hex(),
                        'transit_secret_id': tsid,
                    },
                    'peer_id': identity_id,  # Which identity is sending this
                    'seal_to': peer_id,  # Seal to peer's key
                    'is_outgoing': True,
                    'network_id': network_id,
                    # Surface transit id for local indexing (if a mapper exists)
                    'transit_secret_id': tsid,
                }
                envelopes.append(envelope)

        if envelopes:
            print(f"[sync_request_job] Created {len(envelopes)} sync requests")

        # Update state with current time
        state['last_sync_ms'] = time_now_ms
        return True, state, envelopes

    except Exception as e:
        print(f"[sync_request_job] Error: {e}")
        return False, state, []
