"""
Event encryption/sealing handler and helpers.

Provides:
- EventEncryptHandler: encrypts validated plaintext events (AEAD)
- Seals events to recipient prekey when explicitly requested via hints
"""
from typing import Any, Optional
import json
from core.crypto import encrypt as aead_encrypt
from .crypto_utils import as_bytes, deterministic_nonce, choose_dep
from core.handlers import Handler
import sqlite3
from typing import List


def encrypt_event(envelope: dict[str, Any]) -> dict[str, Any]:
    """Encrypt a validated plaintext event with a single, predictable path.

    Preconditions:
    - envelope.key_secret_id is a 16-byte event-id hex string
    - resolve_deps has attached resolved_deps['key_secret:<id>'].unsealed_secret
    """
    # 1) Read required inputs
    if not isinstance(envelope.get('key_secret_id'), str):
        envelope['error'] = 'missing_key_secret_id'
        return envelope
    key_id = envelope['key_secret_id']
    if not isinstance(envelope.get('event_plaintext'), dict):
        envelope['error'] = 'missing_event_plaintext'
        return envelope

    # 2) Fetch key material from resolved_deps
    dep = choose_dep(envelope.get('resolved_deps') or {}, [f'key_secret:{key_id}'])
    secret = as_bytes((dep or {}).get('unsealed_secret'))
    if not secret:
        envelope['error'] = 'missing_event_key_dep'
        return envelope

    # 3) Canonicalize and encrypt with deterministic nonce
    pt_bytes = json.dumps(envelope['event_plaintext'], sort_keys=True, separators=(',', ':')).encode('utf-8')
    nonce = deterministic_nonce(key_id, pt_bytes, size=24)
    ct, _ = aead_encrypt(pt_bytes, secret, nonce)
    event_ciphertext = nonce + ct

    # 4) Build event_blob: 0x01 || key_id(16) || nonce(24) || ct
    try:
        header = b"\x01" + bytes.fromhex(key_id)
    except Exception:
        envelope['error'] = 'invalid_key_secret_id'
        return envelope
    envelope['event_blob'] = header + event_ciphertext
    envelope['write_to_store'] = True
    return envelope


def seal_event(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    Seal an event to a recipient's public key using libsodium SealedBox.

    - Looks up recipient's public key by peer_id (peers table) or identity_id
      (identities table) from envelope['seal_to'].
    - For sync_request, injects a response transit secret and its id into the
      plaintext before sealing: {'transit_secret','transit_secret_id'}.
    - Places the sealed bytes in envelope['event_sealed'] and removes plaintext.
    - Marks as not stored.
    """
    from core.crypto import seal
    import json

    event_plaintext = envelope.get('event_plaintext')
    if not event_plaintext:
        envelope['error'] = "event_plaintext required for sealing"
        return envelope

    # Fetch recipient public key
    public_key_hex: str | None = None
    # Prefer explicit hint in plaintext if provided by flow
    prekey_pub_hint = event_plaintext.get('prekey_public') if isinstance(event_plaintext, dict) else None
    if isinstance(prekey_pub_hint, (bytes, bytearray)):
        public_key_hex = prekey_pub_hint.hex()
    elif isinstance(prekey_pub_hint, str):
        public_key_hex = prekey_pub_hint

    # Fallback: Use deps_events to pick recipient prekey (must be included by flow deps)
    deps_events = envelope.get('deps_events') or []
    if not public_key_hex and isinstance(deps_events, list):
        for dep in deps_events:
            if not isinstance(dep, dict):
                continue
            pt = dep.get('event_plaintext') or {}
            if isinstance(pt, dict) and pt.get('type') == 'prekey':
                pk = pt.get('public_key')
                if isinstance(pk, str) and pk:
                    public_key_hex = pk
                    break

    if not public_key_hex:
        envelope['error'] = 'recipient_public_key_not_found'
        return envelope

    # Do not inject transit secrets here.
    # Transit secret generation and inclusion (if needed) must be performed by flows/jobs
    # that construct the plaintext prior to sealing.

    # Canonicalize and seal
    pt_bytes = json.dumps(event_plaintext, sort_keys=True, separators=(',', ':')).encode('utf-8')
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
    except Exception:
        envelope['error'] = 'invalid_recipient_public_key'
        return envelope

    sealed = seal(pt_bytes, public_key_bytes)
    # Compute deterministic prekey_secret_id from recipient public key and expose for framing/sending
    try:
        from core.crypto import hash as _blake2
        pk_secret_id = _blake2(public_key_bytes, size=16).hex()
        envelope['prekey_secret_id'] = pk_secret_id
    except Exception:
        pass

    envelope.pop('event_plaintext', None)
    envelope['event_sealed'] = sealed
    envelope['write_to_store'] = False
    return envelope


class EventEncryptHandler(Handler):
    @property
    def name(self) -> str:
        return "event_encrypt"

    def filter(self, envelope: dict[str, Any]) -> bool:
        # Seal outgoing: explicit recipient via seal_to or sync_request with prekey hints
        try:
            if (
                isinstance(envelope.get('event_plaintext'), dict)
                and 'event_sealed' not in envelope
                and (
                    'seal_to' in envelope
                    or (
                        (envelope.get('event_plaintext') or {}).get('type') == 'sync_request'
                        and (
                            isinstance((envelope.get('event_plaintext') or {}).get('prekey_public'), (str, bytes, bytearray))
                            or isinstance((envelope.get('event_plaintext') or {}).get('prekey_secret_id'), str)
                        )
                    )
                )
            ):
                return True
        except Exception:
            pass

        # Encrypt validated event when deps are resolved and a key is specified
        return (
            envelope.get('validated') is True
            and envelope.get('deps_included_and_valid') is True
            and isinstance(envelope.get('event_plaintext'), dict)
            and 'event_blob' not in envelope
            and isinstance(envelope.get('key_secret_id'), str)
        )

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        # Seal first when applicable
        pt = envelope.get('event_plaintext') or {}
        if isinstance(pt, dict) and (
            'seal_to' in envelope or (
                pt.get('type') == 'sync_request'
                and (
                    isinstance(pt.get('prekey_public'), (str, bytes, bytearray))
                    or isinstance(pt.get('prekey_secret_id'), str)
                )
            )
        ):
            env2 = seal_event(envelope)
            return [env2]

        env2 = encrypt_event(envelope)
        return [env2]
