"""User job: broadcast our own bootstrap info (peer, user, address).

This job emits transit-wrapped user bootstrap envelopes for local users
until sync is established. It avoids broadcasting other users.

Behavior:
- Prefer local users (users joined to peers we own). If the `peers` table is
  unavailable (e.g., minimal test DB), fall back to recently joined users.
- Use protocol queries where available (e.g., address lookup).
- Set transit metadata correctly: `transit_secret_id`, `deps`, `dest_ip/port`.
- Include an `address` plaintext envelope derived from the latest address and
  include `key_secret_id` parsed from the stored user ciphertext header.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Any, Tuple, Optional


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    cur = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _load_queries_if_needed() -> None:
    # Ensure protocol queries are available to the job when invoked directly
    from core.queries import query_registry
    try:
        # If our needed query is missing, auto-discover Quiet protocol queries
        if not query_registry.has_query('address.get_latest_for_peer'):
            from core.queries import QueryRegistry
            from pathlib import Path
            proto_dir = str(Path(__file__).parents[2])
            tmp = QueryRegistry(protocol_dir=proto_dir)
            for k, v in tmp._queries.items():  # type: ignore[attr-defined]
                if not query_registry.has_query(k):
                    query_registry.register(k, v)
    except Exception:
        pass


def _parse_key_id_from_user_blob(blob: bytes) -> Optional[str]:
    """Extract key_secret_id hint from a user event ciphertext header.

    Test format: 0x01 | 16-byte key_id | 24-byte nonce | ciphertext...
    Returns hex of the 16-byte key_id if recognizable.
    """
    try:
        if not isinstance(blob, (bytes, bytearray)):
            return None
        if len(blob) < 1 + 16:
            return None
        if blob[0] != 0x01:
            return None
        key_id = bytes(blob[1:17])
        return key_id.hex()
    except Exception:
        return None


def _get_transit_from_projection(db: sqlite3.Connection, network_id: str) -> Optional[Dict[str, Any]]:
    """Fetch transit bootstrap info from the transit_secret projection."""
    try:
        from core.queries import query_registry
        row = query_registry.execute('transit_secret.get_bootstrap_for_network', {
            'network_id': network_id,
        }, db)
        return row
    except Exception:
        return None


def user_job(state: Dict[str, Any], db: sqlite3.Connection, time_now_ms: int) -> Tuple[bool, Dict[str, Any], List[Dict[str, Any]]]:
    """Broadcast our own user bootstrap until peers can sync.

    Emits for each local user (or recent fallback):
      - user (event_blob)
      - peer (event_blob)
      - address (plaintext) including key_secret_id hint
    All with transit metadata set (transit_secret_id, deps, dest_ip/port).
    """
    try:
        # Initialize state and throttle
        if not isinstance(state, dict):
            state = {}
        last = int(state.get('last_broadcast_ms', 0) or 0)
        if time_now_ms - last < 2000:
            return True, state, []

        _load_queries_if_needed()
        from core.queries import query_registry

        cursor = db.cursor()

        local_users: list[tuple[str, str, str]] = []  # (user_id, peer_id, network_id)

        # Preferred: users corresponding to locally-owned peers
        if _table_exists(db, 'peers') and _table_exists(db, 'peer_secrets'):
            try:
                local_peers = query_registry.execute('peer.list_local', {}, db) or []
            except Exception:
                local_peers = []
            for p in local_peers:
                peer_id = p.get('peer_id')
                if not peer_id:
                    continue
                try:
                    rows = query_registry.execute('user.list_for_peer', {
                        'peer_id': peer_id,
                    }, db) or []
                except Exception:
                    rows = []
                if not rows:
                    continue
                r0 = rows[0]
                local_users.append((r0['user_id'], r0['peer_id'], r0['network_id']))

        # Fallback: recently created users (test environments)
        if not local_users:
            rows = cursor.execute(
                """
                SELECT u.user_id, u.peer_id, u.network_id
                FROM users u
                WHERE u.joined_at > ?
                """,
                (time_now_ms - 60000,),
            ).fetchall()
            local_users = [(r['user_id'], r['peer_id'], r['network_id']) for r in rows]

        envelopes: list[dict[str, Any]] = []

        for user_id, peer_id, network_id in local_users:
            # Lookup latest active address via protocol query
            addr = None
            try:
                addr = query_registry.execute('address.get_latest_for_peer', {
                    'peer_id': peer_id,
                    'network_id': network_id,
                }, db)
            except Exception:
                addr = None

            if not addr:
                # No known address to send to; skip for now
                continue

            dest_ip = addr.get('ip')
            dest_port = addr.get('port')

            # Pick transit_secret to use from projection
            transit = _get_transit_from_projection(db, network_id) or {}
            transit_secret_id = transit.get('transit_secret_id')
            # Prefer dest override from transit secret if present
            dest_ip = transit.get('dest_ip') or dest_ip
            dest_port = transit.get('dest_port') or dest_port

            if not (isinstance(transit_secret_id, str) and transit_secret_id):
                # Cannot send without transit key
                continue

            # Fetch stored user/peer blobs from ES
            urow = cursor.execute(
                "SELECT event_blob FROM events WHERE event_id = ?",
                (user_id,),
            ).fetchone()
            prow = cursor.execute(
                "SELECT event_blob FROM events WHERE event_id = ?",
                (peer_id,),
            ).fetchone()

            if not (urow and urow[0] and prow and prow[0]):
                # Missing blobs; skip
                continue

            user_blob = urow[0]
            peer_blob = prow[0]

            # Derive key_secret_id hint from user ciphertext header (for address)
            key_secret_id = _parse_key_id_from_user_blob(user_blob)

            deps = [transit_secret_id]

            # Outgoing peer (event-layer encrypted blob)
            envelopes.append({
                'event_id': peer_id,
                'event_type': 'peer',
                'event_blob': peer_blob,
                'is_outgoing': True,
                'network_id': network_id,
                'peer_id': peer_id,
                'transit_secret_id': transit_secret_id,
                'deps': deps,
                'dest_ip': dest_ip,
                'dest_port': dest_port,
            })

            # Outgoing user (event-layer encrypted blob)
            envelopes.append({
                'event_id': user_id,
                'event_type': 'user',
                'event_blob': user_blob,
                'is_outgoing': True,
                'network_id': network_id,
                'peer_id': peer_id,
                'transit_secret_id': transit_secret_id,
                'deps': deps,
                'dest_ip': dest_ip,
                'dest_port': dest_port,
            })

            # Plaintext address announcement envelope (not stored; for bootstrap)
            if isinstance(dest_ip, str) and isinstance(dest_port, int):
                addr_env: dict[str, Any] = {
                    'event_type': 'address',
                    'event_plaintext': {
                        'type': 'address',
                        'action': 'add',
                        'peer_id': peer_id,
                        'ip': dest_ip,
                        'port': dest_port,
                        'network_id': network_id,
                        'timestamp_ms': time_now_ms,
                        'created_by': peer_id,
                    },
                    'peer_id': peer_id,
                    'network_id': network_id,
                    'is_outgoing': True,
                    'transit_secret_id': transit_secret_id,
                    'deps': deps,
                    'dest_ip': dest_ip,
                    'dest_port': dest_port,
                }
                if isinstance(key_secret_id, str) and key_secret_id:
                    addr_env['key_secret_id'] = key_secret_id
                envelopes.append(addr_env)

        # Update throttle time
        state['last_broadcast_ms'] = time_now_ms

        if envelopes:
            print(f"[user_job] Broadcasting {len(envelopes)} bootstrap envelopes for local users")

        return True, state, envelopes

    except Exception as e:
        print(f"[user_job] Error: {e}")
        import traceback
        traceback.print_exc()
        return False, state, []
