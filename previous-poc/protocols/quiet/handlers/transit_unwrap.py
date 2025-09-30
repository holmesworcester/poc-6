"""
Transit decryption handler and helper.
"""
from typing import Any
import hashlib
import json
from core.crypto import decrypt as aead_decrypt
from .crypto_utils import as_bytes
from core.handlers import Handler
import sqlite3
from typing import List


def unwrap_transit(envelope: dict[str, Any]) -> dict[str, Any]:
    """Decrypt transit layer to reveal event encryption layer.

    Expects resolved_deps to include transit secret for `transit_key_id` under
    `transit_key:<id>`. The transit ciphertext format is: nonce(24) || ciphertext.
    The decrypted JSON carries: event_ciphertext_hex, secret_id (or legacy key_secret_id/event_key_id), network_id.
    """

    # Get transit secret from resolved deps
    resolved = envelope.get('resolved_deps', {})
    tkid = envelope.get('transit_secret_id')
    transit_dep = resolved.get(f'transit_secret:{tkid}') if tkid else None
    if not transit_dep or not transit_dep.get('transit_secret'):
        envelope['error'] = 'missing_transit_secret'
        return envelope

    secret = as_bytes(transit_dep.get('transit_secret'))
    if not secret:
        envelope['error'] = 'invalid_transit_secret'
        return envelope

    data = envelope.get('transit_ciphertext')
    if not isinstance(data, (bytes, bytearray)) or len(data) < 24:
        envelope['error'] = 'invalid_transit_ciphertext'
        return envelope

    nonce, ciphertext = data[:24], data[24:]
    pt = aead_decrypt(ciphertext, secret, nonce)
    try:
        pt_obj = json.loads(pt.decode('utf-8'))
    except Exception as e:
        envelope['error'] = f'transit_plaintext_parse_error: {e}'
        return envelope

    # Extract event layer fields from event_blob
    blob_hex = pt_obj.get('event_blob_hex', '')
    try:
        ev_blob = bytes.fromhex(blob_hex)
    except Exception:
        ev_blob = b''
    if not ev_blob or not isinstance(ev_blob, (bytes, bytearray)):
        envelope['error'] = 'missing_event_blob_in_transit'
        return envelope
    envelope['event_blob'] = ev_blob
    # Parse header: 0x01 | key_id(16) | nonce(24) | ct
    if len(ev_blob) < 1 + 16 + 24 or ev_blob[0] != 0x01:
        envelope['error'] = 'invalid_event_blob_header'
        return envelope
    key_id = bytes(ev_blob[1:1+16]).hex()
    nonce_e = bytes(ev_blob[1+16:1+16+24])
    ct_e = bytes(ev_blob[1+16+24:])
    envelope['key_secret_id'] = key_id

    # id_assign will handle event_id from event_blob
    envelope['write_to_store'] = True
    envelope['deps_included_and_valid'] = False

    # Preserve network metadata
    for field in ['received_at', 'origin_ip', 'origin_port']:
        if field in envelope:
            envelope[field] = envelope[field]

    # Mark transit consumed to prevent re-processing loops
    envelope['transit_decrypted'] = True
    envelope.pop('transit_ciphertext', None)
    envelope.pop('transit_secret_id', None)

    return envelope


class TransitUnwrapHandler(Handler):
    @property
    def name(self) -> str:
        return "transit_unwrap"

    def filter(self, envelope: dict[str, Any]) -> bool:
        # Transit decrypt: incoming with transit encryption
        return (
            envelope.get('deps_included_and_valid') is True
            and 'transit_secret_id' in envelope
            and 'transit_ciphertext' in envelope
            and not envelope.get('transit_decrypted')
        )

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        env2 = unwrap_transit(envelope)
        return [env2]

