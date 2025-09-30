"""
Transit encryption handler and helper.
"""
from typing import Any
import json
import nacl.utils
from core.crypto import encrypt as aead_encrypt
from .crypto_utils import as_bytes, choose_dep
from core.handlers import Handler
import sqlite3
from typing import List


def wrap_transit(envelope: dict[str, Any]) -> dict[str, Any]:
    """Apply transit layer encryption to outgoing envelope.

    Packs event_blob (0x01 | key_id | nonce | ct) into transit plaintext
    and encrypts with XChaCha20-Poly1305 using the transit secret.
    """

    # Extract transit key from resolved_deps
    transit_key_id = envelope['transit_secret_id']
    resolved_deps = envelope.get('resolved_deps') or {}

    transit_key_data = choose_dep(resolved_deps, [f"transit_secret:{transit_key_id}"])
    secret = as_bytes((transit_key_data or {}).get('transit_secret'))
    # Fallback: allow direct secret on envelope for bootstrap flows
    if not secret:
        secret = as_bytes(envelope.get('transit_secret'))
    if not secret:
        return {'error': 'invalid_transit_secret'}

    # Include network_id if provided by upstream
    net_for_payload = envelope.get('network_id')

    # Build plaintext JSON with event layer info
    ev_blob = envelope.get('event_blob')
    if not isinstance(ev_blob, (bytes, bytearray)):
        return {'error': 'missing_event_blob'}
    pt = {
        'event_blob_hex': ev_blob.hex(),
        'network_id': net_for_payload
    }
    pt_bytes = json.dumps(pt, separators=(',', ':'), sort_keys=True).encode('utf-8')

    # Encrypt with random nonce (transit privacy)
    nonce = nacl.utils.random(24)
    ct, _ = aead_encrypt(pt_bytes, secret, nonce)
    transit_ciphertext = nonce + ct

    # Minimal outgoing envelope
    out = {
        'transit_ciphertext': transit_ciphertext,
        'transit_secret_id': transit_key_id,
        'dest_ip': envelope.get('dest_ip', '127.0.0.1'),
        'dest_port': envelope.get('dest_port', 8080),
        'due_ms': envelope.get('due_ms', 0)
    }
    # Attach a local-only hint for the in-process simulator so the receiver
    # can resolve transit_secret without needing a DB row.
    out['transit_secret_hint'] = secret
    out['network_id'] = envelope.get('network_id')
    # Preserve intended local recipient identity if provided by upstream
    if envelope.get('to_peer'):
        out['to_peer'] = envelope.get('to_peer')
    elif envelope.get('peer_id'):
        out['to_peer'] = envelope.get('peer_id')
    return out


class TransitWrapHandler(Handler):
    @property
    def name(self) -> str:
        return "transit_wrap"

    def filter(self, envelope: dict[str, Any]) -> bool:
        # Transit encrypt: outgoing (post event encryption and outgoing check)
        return (
            envelope.get('outgoing_checked') is True
            and 'event_blob' in envelope
            and 'transit_secret_id' in envelope
        )

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        out = wrap_transit(envelope)
        return [out] if out else []

