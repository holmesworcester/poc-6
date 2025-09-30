"""
Projector for transit_secret events (local-only bootstrap state).

Maintains a simple projection keyed by network_id so jobs can look up
the latest transit secret to use for DEM responses and bootstrap sending
without scanning/decrypting local-only ES rows.
"""
from typing import Dict, Any, List


def project(envelope: Dict[str, Any]) -> List[Dict[str, Any]]:
    event = envelope.get('event_plaintext') or {}
    ts_event_id = envelope.get('event_id') or ''
    network_id = event.get('network_id') or ''
    dest_ip = event.get('dest_ip') or event.get('ip') or ''
    dest_port = int(event.get('dest_port') or event.get('port') or 0)
    updated_at = int(event.get('created_at') or 0)

    if not isinstance(network_id, str) or not network_id:
        return []

    deltas: List[Dict[str, Any]] = []
    # Ensure table exists
    deltas.append({
        'sql': (
            """
            CREATE TABLE IF NOT EXISTS transit_bootstrap (
                network_id TEXT PRIMARY KEY,
                transit_secret_id TEXT NOT NULL,
                dest_ip TEXT,
                dest_port INTEGER,
                updated_at INTEGER NOT NULL
            )
            """
        )
    })

    # Upsert latest bootstrap info for this network
    deltas.append({
        'op': 'upsert',
        'table': 'transit_bootstrap',
        'key': {'network_id': network_id},
        'data': {
            'network_id': network_id,
            'transit_secret_id': ts_event_id,
            'dest_ip': dest_ip,
            'dest_port': dest_port,
            'updated_at': updated_at,
        },
    })

    return deltas

