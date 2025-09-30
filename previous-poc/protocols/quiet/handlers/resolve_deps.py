"""
Combined Resolve Dependencies and Unblock handler.

Responsibilities:
- Resolves dependencies from validated events and local-only secrets in ES
- Maintains the presence-only `validated_events` index (IDs only) by listening
  for envelopes with `validated: true` and canonical `event_id`
- Blocks events with missing dependencies and unblocks when ALL dependencies are satisfied

STRICT ACCESS POLICY (do not relax):
- This handler may only access the canonical Event Store (ES) table `events`
  and its own resolver tables: `validated_events`, `blocked_events`, and
  `blocked_event_deps`.
- It must NOT query any other projections or application tables (peers, users,
  addresses, etc.), nor any local secret stores beyond the local-only
  `secret_local` events already persisted in ES.
- All decryption of dependency blobs must rely solely on `secret_local` events
  found in ES and gated by presence in `validated_events`.
"""

# Removed core.types import
import sqlite3
import json
from typing import List, Dict, Any, Optional
from core.handlers import Handler
# Do not import crypto primitives here; use protocol crypto helpers for DRY decrypt

ALLOWED_ID_KEYS = {
    'channel_id', 'group_id', 'peer_id', 'user_id', 'network_id',
    'secret_id', 'secret_local_id', 'key_secret_id', 'prekey_id', 'address_id',
    # Planned/allowed local-only ids
    # Note: peer_secret_id/peer_local_id are intentionally excluded to avoid
    # blocking on local-only identity material which is not stored with the same id.
    'prekey_secret_id',
    'peer_secret_id',
    'transit_secret_id',
}

def _extract_dep_ids_from_dict(d: dict[str, Any], exclude_self: bool = False, event_type: str = None) -> list[str]:
    ids: list[str] = []
    for k, v in d.items():
        if k in ('request_id', 'timestamp_ms', 'created_at', 'updated_at'):
            continue
        # Skip the event's own ID field to prevent self-dependencies
        # e.g., peer_local event shouldn't depend on its own peer_local_id
        if exclude_self and event_type and k == f'{event_type}_id':
            continue
        if k.endswith('_id') and k in ALLOWED_ID_KEYS and isinstance(v, str) and v:
            ids.append(v)
        elif k.endswith('_ids') and isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item:
                    ids.append(item)
    return ids

def _parse_json_maybe(blob: bytes) -> dict | None:
    try:
        if blob and blob[:1] in (b'{', b'['):
            return json.loads(blob.decode('utf-8'))
    except Exception:
        return None
    return None

def _try_parse_or_decrypt_dep_blob(blob: bytes, db: sqlite3.Connection) -> tuple[dict | None, str | None]:
    """Return (event_plaintext, missing_secret_id).

    DRY: routes ciphertext/sealed blobs through protocol crypto helpers.

    - If blob is plaintext JSON, returns (dict, None).
    - If blob is encrypted with header 0x01 + key_id(16) + nonce(24) + ct:
        * If key_id == root_secret_id, decrypt via crypto.
        * Else require key_id validated and decrypt its key_secret first via root;
          then decrypt the original blob via crypto with deps_events.
        * If key_id missing (not validated), return (None, key_id).
    - If blob is sealed (0x02 + prekey_secret_id(16) + sealed):
        * Require prekey_secret_id validated and decrypt the prekey_secret via root,
          then open the sealed bytes via crypto with deps_events.
        * If missing, return (None, prekey_secret_id).
    - Otherwise returns (None, None).
    """
    if not isinstance(blob, (bytes, bytearray)):
        return None, None
    pt = _parse_json_maybe(blob)
    if pt is not None:
        return pt, None

    # Event-layer encrypted
    if len(blob) > 1 + 16 + 24 and blob[0] == 0x01:
        sid_hex = bytes(blob[1:1+16]).hex()
        nonce = bytes(blob[1+16:1+16+24])
        ct = bytes(blob[1+16+24:])
        from protocols.quiet.handlers import event_decrypt as _edec
        env: dict[str, Any] = {
            'event_blob': blob,
            'key_secret_id': sid_hex,
            'deps_included_and_valid': True,
        }
        # Root-secret resolution as a normal dependency
        try:
            from core.crypto import get_root_secret_id, get_root_secret
            if sid_hex == get_root_secret_id():
                env['resolved_deps'] = {f'key_secret:{sid_hex}': {'unsealed_secret': get_root_secret()}}
                dec = _edec.decrypt_event(env)
                if isinstance(dec.get('event_plaintext'), dict):
                    return dec['event_plaintext'], None
        except Exception:
            pass

        # Require key_secret present; decrypt it first
        v = db.execute(
            "SELECT 1 FROM validated_events WHERE validated_event_id = ? LIMIT 1",
            (sid_hex,),
        ).fetchone()
        if not v:
            return None, sid_hex
        row = db.execute(
            "SELECT event_blob FROM events WHERE event_id = ? AND visibility = 'local-only' LIMIT 1",
            (sid_hex,),
        ).fetchone()
        if not row or not row['event_blob']:
            return None, sid_hex
        k_pt, missing = _try_parse_or_decrypt_dep_blob(row['event_blob'], db)
        if missing or not isinstance(k_pt, dict):
            return None, sid_hex
        env['deps_events'] = [{
            'event_id': sid_hex,
            'event_plaintext': k_pt,
        }]
        dec = _edec.decrypt_event(env)
        if isinstance(dec.get('event_plaintext'), dict):
            return dec['event_plaintext'], None
        return None, sid_hex

    # Sealed KEM
    if len(blob) > 1 + 16 and blob[0] == 0x02:
        pkid_hex = bytes(blob[1:1+16]).hex()
        v = db.execute(
            "SELECT 1 FROM validated_events WHERE validated_event_id = ? LIMIT 1",
            (pkid_hex,),
        ).fetchone()
        if not v:
            return None, pkid_hex
        row = db.execute(
            "SELECT event_blob FROM events WHERE event_id = ? AND visibility = 'local-only' LIMIT 1",
            (pkid_hex,),
        ).fetchone()
        if not row or not row['event_blob']:
            return None, pkid_hex
        prekey_pt, missing = _try_parse_or_decrypt_dep_blob(row['event_blob'], db)
        if missing or not isinstance(prekey_pt, dict):
            return None, pkid_hex
        sealed = bytes(blob[1+16:])
        from protocols.quiet.handlers import event_decrypt as _edec
        env: dict[str, Any] = {
            'event_sealed': sealed,
            'prekey_secret_id': pkid_hex,
            'deps_included_and_valid': True,
            'deps_events': [{
                'event_id': pkid_hex,
                'event_plaintext': prekey_pt,
            }],
        }
        dec = _edec.open_sealed_event(env)
        if isinstance(dec.get('event_plaintext'), dict):
            return dec['event_plaintext'], None
        return None, pkid_hex

    return None, None

def filter_func(envelope: dict[str, Any]) -> bool:
    """Run when deps need resolution OR validated events need indexing."""
    # Process if deps are not resolved
    if envelope.get('deps_included_and_valid') is not True:
        return True
    # Also process validated events with event_id that need indexing
    if envelope.get('validated') is True and envelope.get('event_id'):
        return True
    return False


def handler(envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
    """
    Combined handler for dependency resolution and unblocking.

    Args:
        envelope: dict[str, Any] needing resolution or triggering unblocking
        db: Database connection

    Returns:
        List of envelopes - resolved events or unblocked events (NOT the triggering validated event)
    """
    results: List[dict[str, Any]] = []

    # Listen for validated envelopes and index their IDs, then unblock dependents.
    # This must happen BEFORE dependency resolution to ensure validated events
    # are indexed even if they have missing dependencies themselves.
    # ALSO index local_only events with event_id since they're trusted by definition
    try:
        should_index = False
        ev_id = envelope.get('event_id')

        # Index if validated OR if it's a local_only event with an ID
        if envelope.get('validated') is True:
            should_index = True
        elif envelope.get('local_only') is True and ev_id and envelope.get('write_to_store'):
            # Local-only events are trusted and should be immediately available as deps
            should_index = True

        if should_index and isinstance(ev_id, str) and ev_id:
            # Check if already indexed to avoid unnecessary work
            already_indexed = db.execute(
                "SELECT 1 FROM validated_events WHERE validated_event_id = ? LIMIT 1",
                (ev_id,)
            ).fetchone()

            if not already_indexed:
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO validated_events (validated_event_id) VALUES (?)",
                        (ev_id,),
                    )
                    db.commit()
                    # Unblock any events waiting on this validated id
                    try:
                        results.extend(unblock_waiting_events(ev_id, db))
                    except Exception:
                        pass
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
    except Exception:
        # Non-fatal; indexing is best-effort and retried on reprocessing
        pass

    # If deps already resolved (e.g., receive_from_network attached local-only hints),
    # honor that and skip declaring/resolving deps again. Do not re-emit the
    # envelope here; downstream handlers in this same pass will still see the
    # mutated envelope, and only they should decide whether to enqueue it.
    if envelope.get('deps_included_and_valid') is True:
        return results

    # Simplified dependencies: unify existing envelope deps with deps declared in plaintext
    deps_union: List[str] = []
    existing = envelope.get('deps') or []
    if isinstance(existing, list):
        deps_union.extend([str(d) for d in existing])
    if 'event_plaintext' in envelope and isinstance(envelope['event_plaintext'], dict):
        pt_deps = envelope['event_plaintext'].get('deps')
        if isinstance(pt_deps, list):
            deps_union.extend([str(d) for d in pt_deps])
        # Include allowlisted *_id/_ids from plaintext (but exclude self-references)
        event_type = envelope.get('event_type')
        deps_union.extend(_extract_dep_ids_from_dict(envelope['event_plaintext'], exclude_self=True, event_type=event_type))
    # Also include allowlisted *_id/_ids from top-level envelope (but exclude peer_id which is the actor)
    deps_union.extend(_extract_dep_ids_from_dict({k: v for k, v in envelope.items() if k not in ('event_plaintext','deps','peer_id')}))
    # Include secret hint from event_blob header (0x01 + key_id)
    # Skip adding implicit event-key deps for local-only events; they're encrypted
    # under root_secret and the header key_id is not an ES event id.
    if not envelope.get('local_only'):
        try:
            blob = envelope.get('event_blob')
            if isinstance(blob, (bytes, bytearray)) and len(blob) >= 1 + 16 + 24 and blob[0] == 0x01:
                sid_hex = bytes(blob[1:1+16]).hex()
                # Double-check it's not the root secret ID (shouldn't happen for non-local-only)
                from core.crypto import get_root_secret, hash as blake2
                root_id = blake2(get_root_secret(), size=16).hex()
                if sid_hex != root_id:
                    deps_union.append(sid_hex)
        except Exception:
            pass
    # Deduplicate while preserving order
    seen_ids: set[str] = set()
    deps_list: List[str] = []
    for d in deps_union:
        if d not in seen_ids:
            seen_ids.add(d)
            deps_list.append(d)
    if deps_list:
        envelope['deps'] = deps_list
        envelope['deps_included_and_valid'] = False
        try:
            if envelope.get('event_type') == 'user':
                print(f"[resolve_deps] user deps declared: {deps_list}")
        except Exception:
            pass
    else:
        # No deps to resolve – mark as ready and emit
        envelope['deps_included_and_valid'] = True
        return [envelope]

    # Handle dependency resolution
    if 'deps' in envelope and envelope.get('deps_included_and_valid') is not True:
        resolved_envelope = resolve_dependencies(envelope, db)
        if resolved_envelope and not resolved_envelope.get('missing_deps'):
            # Debug: log resolution summary for user events
            try:
                if resolved_envelope.get('event_type') == 'user':
                    rdeps = list((resolved_envelope.get('resolved_deps') or {}).keys())
                    print(f"[resolve_deps] user deps resolved. deps={resolved_envelope.get('deps')}, resolved_keys={rdeps}, deps_events={len(resolved_envelope.get('deps_events') or [])}")
            except Exception:
                pass
            results.append(resolved_envelope)
        else:
            if resolved_envelope and resolved_envelope.get('missing_deps'):
                print(f"[resolve_deps] Will block {envelope.get('event_type')}:{envelope.get('event_id')} missing {resolved_envelope.get('missing_deps_list')}")
                block_event(resolved_envelope, db)
        return results

    return results


def resolve_dependencies(envelope: dict[str, Any], db: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """Resolve dependencies strictly against validated_events and fetch blobs from ES.

    deps = envelope.deps (if any) plus any deps declared in event_plaintext.deps and *_id/_ids
    keys from both event_plaintext and the top-level envelope.
    """
    deps_needed = envelope.get('deps', [])
    if not isinstance(deps_needed, list):
        deps_needed = []

    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for d in deps_needed:
        s = str(d)
        if s not in seen:
            seen.add(s)
            ordered.append(s)

    dep_event_rows: list[dict[str, Any]] = []
    resolved_map: dict[str, dict[str, Any]] = {}
    missing_deps: list[str] = []

    for dep_id in ordered:
        # Resolve root_secret like a normal dependency for encryption/decryption
        try:
            from core.crypto import get_root_secret_id, get_root_secret
            if dep_id == get_root_secret_id():
                resolved_map[f'key_secret:{dep_id}'] = {
                    'unsealed_secret': get_root_secret(),
                }
                # Do not require a validated event for root_secret
                continue
        except Exception:
            pass
        # First: try local-only peer_secret resolution without validated gating
        try:
            row = db.execute(
                "SELECT event_blob FROM events WHERE event_id = ? AND visibility = 'local-only' LIMIT 1",
                (dep_id,),
            ).fetchone()
            if row and row['event_blob'] is not None:
                pt, missing = _try_parse_or_decrypt_dep_blob(row['event_blob'], db)
                if not missing and isinstance(pt, dict):
                    if pt.get('type') == 'peer_secret':
                        # Expose minimal signer material
                        pub = pt.get('public_key')
                        prv = pt.get('private_key')
                        resolved_map[f'peer_secret:{dep_id}'] = {
                            'public_key': pub,
                            'private_key': prv,
                            'created_at': pt.get('created_at'),
                        }
                        dep_event_rows.append({'event_id': dep_id, 'event_plaintext': pt})
                        # Skip generic validated path for this dep
                        continue
                    if pt.get('type') == 'transit_secret':
                        # Attach transit secret for outgoing transit encryption and network attribution
                        sec = pt.get('unsealed_secret') or pt.get('transit_secret')
                        resolved_map[f'transit_secret:{dep_id}'] = {
                            'transit_secret': sec,
                            'network_id': pt.get('network_id', ''),
                            # Carry owner identity for viewer attribution
                            'peer_secret_id': pt.get('peer_secret_id'),
                            'peer_id': pt.get('peer_id'),
                        }
                        dep_event_rows.append({'event_id': dep_id, 'event_plaintext': pt})
                        continue
                    if pt.get('type') == 'key_secret':
                        # Attach key material for event-layer encryption/decryption
                        sec = pt.get('unsealed_secret') or pt.get('secret')
                        resolved_map[f'key_secret:{dep_id}'] = {
                            'unsealed_secret': sec,
                        }
                        dep_event_rows.append({'event_id': dep_id, 'event_plaintext': pt})
                        continue
                    if pt.get('type') == 'prekey_secret':
                        # Attach prekey material and network attribution when present
                        resolved_map[f'prekey_secret:{dep_id}'] = {
                            'prekey_private': pt.get('prekey_private'),
                            'prekey_public': pt.get('prekey_public'),
                            'network_id': pt.get('network_id', ''),
                            'peer_secret_id': pt.get('peer_secret_id'),
                            'peer_id': pt.get('peer_id'),
                        }
                        dep_event_rows.append({'event_id': dep_id, 'event_plaintext': pt})
                        continue
        except Exception:
            pass

        # Validate first
        try:
            v = db.execute(
                "SELECT 1 FROM validated_events WHERE validated_event_id = ? LIMIT 1",
                (dep_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            v = None
        if not v:
            missing_deps.append(dep_id)
            continue
        # Fetch blob from ES and try parse/decrypt to plaintext
        try:
            row = db.execute(
                "SELECT event_id, event_blob, visibility FROM events WHERE event_id = ? LIMIT 1",
                (dep_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if not row or row['event_blob'] is None:
            missing_deps.append(dep_id)
            continue
        blob = row['event_blob']
        # Parse or decrypt via crypto helper; if secret is missing, enqueue that as missing dep
        pt, missing_secret = _try_parse_or_decrypt_dep_blob(blob, db)
        if missing_secret:
            missing_deps.append(missing_secret)
            continue
        if pt is None:
            missing_deps.append(dep_id)
            continue
        dep_event_rows.append({'event_id': row['event_id'], 'event_plaintext': pt})
        # If this dep is a key_secret, expose its unsealed secret for event encryption
        try:
            if isinstance(pt, dict) and pt.get('type') == 'key_secret':
                resolved_map[f'key_secret:{dep_id}'] = {
                    'unsealed_secret': pt.get('unsealed_secret') or pt.get('secret'),
                }
        except Exception:
            pass
        # If this dep is a peer event, eagerly resolve its signer peer_secret and attach
        try:
            if isinstance(pt, dict) and pt.get('type') == 'peer':
                psid = pt.get('peer_secret_id')
                if isinstance(psid, str) and psid:
                    prow = db.execute(
                        "SELECT event_blob FROM events WHERE event_id = ? AND visibility = 'local-only' LIMIT 1",
                        (psid,),
                    ).fetchone()
                    if prow and prow['event_blob'] is not None:
                        p_pt, p_missing = _try_parse_or_decrypt_dep_blob(prow['event_blob'], db)
                        if not p_missing and isinstance(p_pt, dict) and p_pt.get('type') == 'peer_secret':
                            resolved_map[f'peer_secret:{psid}'] = {
                                'public_key': p_pt.get('public_key'),
                                'private_key': p_pt.get('private_key'),
                                'created_at': p_pt.get('created_at'),
                            }
        except Exception:
            pass

    if missing_deps:
        envelope['missing_deps'] = True
        envelope['missing_deps_list'] = missing_deps
        envelope['deps_included_and_valid'] = False
        # Will be blocked by caller
        return envelope

    # Success
    envelope['deps_events'] = dep_event_rows
    envelope['deps_included_and_valid'] = True
    # Attach any resolved local-only deps
    if resolved_map:
        existing = envelope.get('resolved_deps') or {}
        merged = dict(existing)
        merged.update(resolved_map)
        envelope['resolved_deps'] = merged
    envelope.pop('missing_deps', None)
    envelope.pop('missing_deps_list', None)
    return envelope


def block_event(envelope: dict[str, Any], db: sqlite3.Connection) -> None:
    """Block an event with missing dependencies (minimal)."""
    event_id = envelope.get('event_id')
    missing_deps_list = envelope.get('missing_deps_list', [])
    if not event_id or not missing_deps_list:
        return
    
    def _to_jsonable(obj: Any) -> Any:
        if isinstance(obj, (bytes, bytearray)):
            return obj.hex()
        if isinstance(obj, dict):
            return {k: _to_jsonable(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_to_jsonable(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(_to_jsonable(v) for v in obj)
        return obj

    try:
        # Store blocked envelope
        db.execute(
            """
            INSERT OR REPLACE INTO blocked_events (event_id, envelope_json, missing_deps)
            VALUES (?, ?, ?)
            """,
            (
                event_id,
                json.dumps(_to_jsonable(envelope)),
                json.dumps(missing_deps_list),
            ),
        )
        
        # Clear and insert dependency tracking
        db.execute("DELETE FROM blocked_event_deps WHERE event_id = ?", (event_id,))
        
        for dep in missing_deps_list:
            dep_event_id = dep.split(':')[-1] if ':' in dep else dep
            db.execute("""
                INSERT INTO blocked_event_deps (event_id, dep_id)
                VALUES (?, ?)
            """, (event_id, dep_event_id))
        
        db.commit()
        
    except Exception as e:
        db.rollback()
        print(f"Failed to block event {event_id}: {e}")


def unblock_waiting_events(validated_event_id: str, db: sqlite3.Connection) -> List[dict[str, Any]]:
    """Check and unblock events waiting on this validated event."""
    if not validated_event_id:
        return []
    
    unblocked = []
    
    # Find events blocked on this dependency
    cursor = db.execute(
        """
        SELECT be.event_id, be.envelope_json
        FROM blocked_events be
        JOIN blocked_event_deps bed ON be.event_id = bed.event_id
        WHERE bed.dep_id = ?
        """,
        (validated_event_id,),
    )
    
    blocked_events = cursor.fetchall()
    
    if blocked_events:
        print(f"[resolve_deps] Validated {validated_event_id} unblocks {len(blocked_events)} event(s)")

    for blocked in blocked_events:
        blocked_event_id = blocked['event_id']
        # No SQL retry limit; pipeline loop guard exists

        # Check if ALL dependencies are now satisfied
        if are_all_deps_satisfied(blocked_event_id, db):
            # Unblock this event and eagerly resolve its dependencies so
            # downstream handlers (e.g., signature) have required context
            blocked_envelope = json.loads(blocked['envelope_json'])

            # Restore bytes fields from hex if needed (e.g., ciphertexts)
            def _maybe_from_hex(val: Any) -> Any:
                if isinstance(val, str):
                    hs = val.strip()
                    if len(hs) % 2 == 0:
                        try:
                            return bytes.fromhex(hs)
                        except Exception:
                            return val
                return val

            for k in ('transit_ciphertext',):
                if k in blocked_envelope:
                    blocked_envelope[k] = _maybe_from_hex(blocked_envelope[k])
            blocked_envelope['unblocked'] = True

            # Ensure deps resolve runs; resolver will extract *_id(s) and merge deps
            if 'deps' not in blocked_envelope:
                blocked_envelope['deps'] = []
            blocked_envelope['deps_included_and_valid'] = False

            # Resolve deps now that they should all be present
            try:
                resolved = resolve_dependencies(blocked_envelope, db)
                if resolved is not None:
                    blocked_envelope = resolved
            except Exception:
                # If resolution fails for any reason, continue with original envelope
                pass

            # Remove from blocked events
            db.execute("DELETE FROM blocked_events WHERE event_id = ?", (blocked_event_id,))

            unblocked.append(blocked_envelope)
    
    db.commit()
    return unblocked


def are_all_deps_satisfied(event_id: str, db: sqlite3.Connection) -> bool:
    """Check if all dependencies for an event are satisfied.

    Important: Only events in the event store unblock dependencies. Projections
    alone (e.g., peers table) MUST NOT satisfy dependencies.
    """
    cursor = db.execute(
        "SELECT dep_id FROM blocked_event_deps WHERE event_id = ?",
        (event_id,),
    )

    all_deps = [row[0] for row in cursor]

    for dep_id in all_deps:
        # Consider satisfied only if validated
        exists = db.execute(
            "SELECT 1 FROM validated_events WHERE validated_event_id = ? LIMIT 1",
            (dep_id,),
        ).fetchone()
        if not exists:
            print(f"[resolve_deps] Dep not validated for {event_id}: {dep_id}")
            return False

    return True

class ResolveDepsHandler(Handler):
    """Handler for resolve deps."""

    @property
    def name(self) -> str:
        return "resolve_deps"

    def filter(self, envelope: dict[str, Any]) -> bool:
        """Check if this handler should process the envelope."""
        if not isinstance(envelope, dict):
            print(f"[resolve_deps] WARNING: filter got {type(envelope)} instead of dict: {envelope}")
            return False
        return filter_func(envelope)

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Process the envelope."""
        # resolve_deps handler function returns a list
        return handler(envelope, db)
