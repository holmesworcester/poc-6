"""
Signature handler - Handles both signing and verification of events.

Rules:
- Sign only when deps are resolved AND signer material is present (peer_secret in resolved_deps).
- Do not set errors for signing readiness; simply skip until ready.
- Verify incoming events after deps resolve; verification may set errors.
"""
from typing import Any, List
import sqlite3
import json
from core.handlers import Handler


def canonicalize_event(event_plaintext: dict) -> bytes:
    """Deterministic JSON for signing/verification (stub for canonical 512-byte format)."""
    return json.dumps(event_plaintext, sort_keys=True).encode('utf-8')


def filter_func(envelope: dict[str, Any]) -> bool:
    """Decide whether to sign or verify for this envelope."""
    et = envelope.get('event_type')
    # Skip non-signed types
    if et in ('key', 'peer_secret', 'key_secret', 'prekey_secret', 'sync_request'):
        return False
    # Never process envelopes already in error
    if envelope.get('error'):
        return False

    # Sign: self-created, plaintext present, no signature yet, deps resolved, signer present
    if envelope.get('self_created') is True and isinstance(envelope.get('event_plaintext'), dict):
        if not envelope['event_plaintext'].get('signature') and envelope.get('deps_included_and_valid') is True:
            resolved = envelope.get('resolved_deps') or {}
            has_signer = any(
                isinstance(k, str) and k.startswith('peer_secret:') and isinstance(v, dict) and v.get('private_key')
                for k, v in resolved.items()
            )
            if et in ('network', 'group', 'user'):
                print(f"[signature.filter_func] {et} event: deps_valid={envelope.get('deps_included_and_valid')}, has_signer={has_signer}, resolved_deps_keys={list(resolved.keys())}")
            if has_signer:
                return True

    # Verify: plaintext present, not yet checked, deps resolved
    if isinstance(envelope.get('event_plaintext'), dict) and envelope.get('sig_checked') is not True and envelope.get('deps_included_and_valid') is True:
        return True

    return False


def handler(envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
    """Sign self-created events or verify signatures on incoming ones."""
    if envelope.get('event_type') in ('network', 'group', 'user'):
        print(f"[signature.handler] Processing {envelope.get('event_type')}, self_created={envelope.get('self_created')}, has_sig={bool((envelope.get('event_plaintext') or {}).get('signature'))}")
    if envelope.get('self_created') and not (envelope.get('event_plaintext') or {}).get('signature'):
        return sign_event(envelope)
    return verify_signature(envelope)


def sign_event(envelope: dict[str, Any]) -> List[dict[str, Any]]:
    """Sign using peer_secret from resolved_deps (no DB reads)."""
    event_plaintext = envelope.get('event_plaintext', {})
    rd = envelope.get('resolved_deps') or {}
    if envelope.get('event_type') in ('network', 'group', 'user'):
        print(f"[signature.sign_event] {envelope.get('event_type')} event resolved_deps keys: {list(rd.keys())}")
    signer = None
    for k, v in rd.items():
        if isinstance(k, str) and k.startswith('peer_secret:') and isinstance(v, dict) and v.get('private_key'):
            signer = v
            break
    if not signer:
        # Not ready; let resolve_deps requeue when signer is attached
        return []

    # Build canonical without signature and sign
    event_copy = dict(event_plaintext)
    event_copy.pop('signature', None)
    canonical = canonicalize_event(event_copy)

    try:
        from core.crypto import sign
        pk_hex = signer.get('private_key')
        if isinstance(pk_hex, (bytes, bytearray)):
            pk_b = bytes(pk_hex)
        else:
            pk_b = bytes.fromhex(pk_hex)
        sig = sign(canonical, pk_b).hex()
    except Exception as e:
        # Signing failures are terminal for this envelope
        envelope['error'] = str(e)
        envelope['sig_failed'] = True
        return [envelope]

    envelope['event_plaintext']['signature'] = sig
    envelope['sig_checked'] = True
    envelope['self_signed'] = True
    return [envelope]


def verify_signature(envelope: dict[str, Any]) -> List[dict[str, Any]]:
    """Verify signature using peer dep (or self-attested for peer events)."""
    from core import crypto
    pt = envelope.get('event_plaintext', {})
    sig = pt.get('signature')
    if not sig:
        envelope['error'] = 'No signature in event'
        envelope['sig_checked'] = True
        envelope['sig_failed'] = True
        return [envelope]

    peer_id = pt.get('peer_id')
    pub = None
    if peer_id and envelope.get('resolved_deps'):
        peer_dep = envelope['resolved_deps'].get(f'peer:{peer_id}')
        if isinstance(peer_dep, dict) and isinstance(peer_dep.get('event_plaintext'), dict):
            pub = peer_dep['event_plaintext'].get('public_key')
    if not pub and pt.get('type') == 'peer':
        pub = pt.get('public_key')
    if not pub:
        envelope['error'] = f"Cannot verify signature: peer dependency not resolved for {pt.get('type')} event"
        envelope['sig_checked'] = True
        envelope['sig_failed'] = True
        return [envelope]

    try:
        canonical = canonicalize_event({k: v for k, v in pt.items() if k != 'signature'})
        sig_b = bytes.fromhex(sig)
        pub_b = bytes.fromhex(pub)
        if crypto.verify(canonical, sig_b, pub_b):
            envelope['sig_checked'] = True
        else:
            envelope['error'] = 'Signature verification failed'
            envelope['sig_checked'] = True
            envelope['sig_failed'] = True
    except Exception as e:
        envelope['error'] = f'Signature verification error: {e}'
        envelope['sig_checked'] = True
        envelope['sig_failed'] = True

    if pt.get('type') == 'peer':
        envelope['peer_id'] = envelope.get('event_id')
    return [envelope]


class SignatureHandler(Handler):
    @property
    def name(self) -> str:
        return 'signature'

    def filter(self, envelope: dict[str, Any]) -> bool:
        return filter_func(envelope)

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        result = handler(envelope, db)
        if result:
            return [result]
        return []

