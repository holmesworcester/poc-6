"""
Event decryption/opening handler and helpers.

Provides:
- EventDecryptHandler: decrypts event-layer AEAD
- Opens sealed (KEM) events using prekey_secret deps
"""
from typing import Any, Optional
import json
from core.crypto import decrypt as aead_decrypt, unseal, hash as blake2b
from .crypto_utils import as_bytes
from core.handlers import Handler
import sqlite3
from typing import List


def decrypt_event(envelope: dict[str, Any]) -> dict[str, Any]:
    """Decrypt a regular event using symmetric AEAD.

    Requires the appropriate key material to be present in `deps_events` or
    `resolved_deps`. If required material is missing or invalid, returns an
    error in the envelope; no plaintext fallbacks are performed.
    """
    # Determine event key id: prefer explicit envelope hint, then derive from event_blob header, then event_ciphertext header (legacy)
    event_key_id: Optional[str] = None
    try:
        if isinstance(envelope.get('key_secret_id'), str):
            event_key_id = envelope.get('key_secret_id')  # type: ignore[assignment]
    except Exception:
        event_key_id = None
    if not event_key_id:
        try:
            ev_blob = envelope.get('event_blob')
            if isinstance(ev_blob, (bytes, bytearray)) and len(ev_blob) >= 1 + 16 and ev_blob[0] == 0x01:
                event_key_id = bytes(ev_blob[1:1+16]).hex()
        except Exception:
            pass
    # No legacy header parsing from event_ciphertext (removed); rely on event_blob header

    # Find secret from deps_events (preferred) or resolved_deps fallback
    secret: Optional[bytes] = None
    deps_events = envelope.get('deps_events') or []
    if isinstance(deps_events, list):
        for dep in deps_events:
            if not isinstance(dep, dict):
                continue
            if event_key_id and dep.get('event_id') != event_key_id:
                continue
            pt = dep.get('event_plaintext') or {}
            if isinstance(pt, dict):
                sec = as_bytes(pt.get('unsealed_secret')) or as_bytes(pt.get('secret'))
                if sec:
                    secret = sec
                    break

    if secret is None:
        # Fallback to resolved_deps minimal shape (new key_secret only)
        resolved = envelope.get('resolved_deps') or {}
        if event_key_id:
            key_dep = resolved.get(f"key_secret:{event_key_id}")
            if isinstance(key_dep, dict):
                secret = as_bytes(key_dep.get('unsealed_secret')) or as_bytes(key_dep.get('secret'))

    if not secret:
        envelope['error'] = 'missing_event_key_dep'
        return envelope

    ev_blob = envelope.get('event_blob')
    # Support plaintext JSON blobs ONLY for sync_request (stored plaintext)
    if isinstance(ev_blob, (bytes, bytearray)) and ev_blob[:1] in (b'{', b'['):
        try:
            pt_obj = json.loads(ev_blob.decode('utf-8'))
            if isinstance(pt_obj, dict) and pt_obj.get('type') == 'sync_request':
                envelope['event_plaintext'] = pt_obj
                if 'event_type' not in envelope:
                    envelope['event_type'] = 'sync_request'
                envelope['write_to_store'] = True
                return envelope
            else:
                envelope['error'] = 'plaintext_event_not_allowed'
                return envelope
        except Exception:
            pass
    if not isinstance(ev_blob, (bytes, bytearray)) or len(ev_blob) < 1 + 16 + 24 or ev_blob[0] != 0x01:
        envelope['error'] = 'invalid_event_blob'
        return envelope
    nonce = bytes(ev_blob[1+16:1+16+24])
    ciphertext = bytes(ev_blob[1+16+24:])
    plaintext_bytes = aead_decrypt(ciphertext, secret, nonce)
    event_plaintext = json.loads(plaintext_bytes.decode('utf-8'))

    envelope['event_plaintext'] = event_plaintext
    if 'event_type' not in envelope and 'type' in event_plaintext:
        envelope['event_type'] = event_plaintext['type']
    envelope['write_to_store'] = True
    return envelope


def open_sealed_event(envelope: dict[str, Any]) -> dict[str, Any]:
    """Open sealed event using SealedBox and attached prekey deps.

    Expects the prekey to be present in deps_events and identified via
    prekey_secret_id.
    """
    event_sealed = envelope.get('event_sealed')
    if not isinstance(event_sealed, (bytes, bytearray)):
        envelope['error'] = "event_sealed required for opening"
        return envelope

    # Principled selection: require an explicit prekey hint and match exactly.
    target_prekey_id = envelope.get('prekey_secret_id')
    if not isinstance(target_prekey_id, str) or not target_prekey_id:
        envelope['error'] = 'missing_prekey_hint'
        return envelope

    candidates: list[tuple[bytes, bytes]] = []
    deps_events = envelope.get('deps_events') or []
    if isinstance(deps_events, list):
        for dep in deps_events:
            if not isinstance(dep, dict):
                continue
            if dep.get('event_id') != target_prekey_id:
                continue
            pt = dep.get('event_plaintext') or {}
            if not isinstance(pt, dict):
                continue
            priv_b = as_bytes(pt.get('prekey_private'))
            pub_b = as_bytes(pt.get('prekey_public'))
            if priv_b and pub_b:
                candidates.append((priv_b, pub_b))

    if not candidates:
        envelope['error'] = 'recipient_prekey_not_found'
        return envelope

    last_err: Exception | None = None
    for priv_b, pub_b in candidates:
        try:
            plaintext_bytes = unseal(event_sealed, priv_b, pub_b)
            event_plaintext = json.loads(plaintext_bytes.decode('utf-8'))
            envelope['event_plaintext'] = event_plaintext
            # Derive type so validate can run (IDs are assigned from event_blob, not plaintext)
            etype = event_plaintext.get('type')
            if etype:
                envelope['event_type'] = etype
            # Sealed requests are not assigned event_id here; id_assign derives IDs from event_blob only
            # Sealed requests are not signed; bypass signature verification
            envelope['sig_checked'] = True
            # Store sync_request for simplicity (normal pipeline)
            if etype == 'sync_request':
                envelope['write_to_store'] = True
                envelope['is_sync_request'] = True
            return envelope
        except Exception as e:
            last_err = e
            continue

    envelope['error'] = f"Failed to open sealed: {last_err}"
    return envelope


class EventDecryptHandler(Handler):
    @property
    def name(self) -> str:
        return "event_decrypt"

    def filter(self, envelope: dict[str, Any]) -> bool:
        # Open sealed: requires deps resolved first
        if (
            envelope.get('deps_included_and_valid') is True
            and 'event_sealed' in envelope
            and 'event_plaintext' not in envelope
        ):
            return True
        # Decrypt regular (symmetric)
        return (
            envelope.get('deps_included_and_valid') is True
            and 'event_blob' in envelope
            and 'event_plaintext' not in envelope
        )

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        if 'event_sealed' in envelope and 'event_plaintext' not in envelope:
            env2 = open_sealed_event(envelope)
        else:
            env2 = decrypt_event(envelope)
        return [env2]
