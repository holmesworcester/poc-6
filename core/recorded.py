"""Recorded event management functions.

ARCHITECTURE NOTE: Event Type Registry
======================================
Each event module declares its own metadata via module-level constants:

    EVENT_TYPE = 'message'
    SHAREABLE = True
    EVENT_SPEC = { ... }  # projector specification

The registry (events/registry.py) auto-discovers these declarations and provides:
    - is_shareable(event_type) - whether event syncs to other peers
    - get_event_spec(event_type) - projector specification
    - get_project_pure_fn(event_type) - pure projector function

Benefits:
    - Adding new event type: just create module with metadata + project_pure()
    - No more forgotten updates to LOCAL_ONLY_TYPES
    - Metadata lives with the code it describes
    - Easy to audit: grep -r "SHAREABLE = True" events/

See docs/planning/event-registry-design.md for full design rationale.
"""
from typing import Any
import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from events import registry
from core import store
from core import crypto
from core import wire_format
from core.db import create_safe_db, create_unsafe_db
from core.projection import resolver
from core.projection.apply import apply_writes

log = logging.getLogger(__name__)


def _verify_signature_task(task: tuple[str, bytes, bytes, bytes]) -> tuple[str, bool]:
    ref_id, signed_bytes, signature, public_key = task
    try:
        return ref_id, crypto.verify(signed_bytes, signature, public_key)
    except Exception:
        return ref_id, False


def _verify_signatures_parallel(
    tasks: list[tuple[str, bytes, bytes, bytes]]
) -> dict[str, bool]:
    if not tasks:
        return {}
    workers = int(os.getenv("SIGNATURE_VERIFY_THREADS", "0"))
    if workers <= 0:
        workers = min(8, os.cpu_count() or 4)
    if workers <= 1 or len(tasks) < 2:
        return {ref_id: ok for ref_id, ok in map(_verify_signature_task, tasks)}

    results: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for ref_id, ok in executor.map(_verify_signature_task, tasks, chunksize=200):
            results[ref_id] = ok
    return results


def _pre_projection_gate(
    ctx: dict[str, Any],
    db: Any,
    skip_negentropy: bool,
) -> tuple[bool, list[str | None]]:
    from core import queues

    recorded_id = ctx.get("recorded_id")
    ref_id = ctx.get("ref_id")
    recorded_by = ctx.get("recorded_by")
    recorded_at = ctx.get("recorded_at", 0)
    event_data = ctx.get("event_data")
    event_type = ctx.get("event_type")
    missing_key_ids = ctx.get("missing_key_ids") or []

    if not recorded_id or not ref_id or not recorded_by:
        return False, [None, None]

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Check if event is pre-deleted
    if event_type:
        event_spec_early = registry.get_event_spec(event_type)
        if event_spec_early and event_spec_early.get('skip_if_deleted'):
            deleted_check = safedb.query_one(
                "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
                (ref_id, recorded_by)
            )
            if deleted_check:
                safedb.execute(
                    "DELETE FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
                    (ref_id, recorded_by)
                )
                unsafedb.execute("DELETE FROM store WHERE id = ?", (ref_id,))
                return False, [None, recorded_id]

    should_mark_shareable = False
    if event_type:
        should_mark_shareable = registry.is_shareable(event_type)
    elif missing_key_ids:
        should_mark_shareable = True

    if should_mark_shareable:
        from events.network import negentropy
        negentropy.add_shareable_event(
            ref_id,
            recorded_by,
            created_at=None,
            recorded_at=recorded_at,
            db=db,
            skip_negentropy=skip_negentropy
        )

    if missing_key_ids:
        queues.blocked.add(recorded_id, recorded_by, missing_key_ids, safedb)
        return False, [None, recorded_id]

    if not event_data:
        return False, [None, recorded_id]

    return True, [None, recorded_id]


def _apply_projection(
    ctx: dict[str, Any],
    resolve_result: Any,
    project_pure_fn: Any,
    db: Any,
    _recursion_depth: int,
    skip_negentropy: bool,
) -> list[str | None]:
    from core import queues

    recorded_id = ctx.get("recorded_id")
    ref_id = ctx.get("ref_id")
    recorded_by = ctx.get("recorded_by")
    recorded_at = ctx.get("recorded_at", 0)
    event_data = ctx.get("event_data")
    event_type = ctx.get("event_type")

    if not recorded_id or not ref_id or not recorded_by:
        return [None, None]

    safedb = create_safe_db(db, recorded_by=recorded_by)

    try:
        projector_result = project_pure_fn(resolve_result.ctx)
    except Exception as e:
        log.error(f"[PROJECTION_FAILED] project_pure raised for {ref_id[:20]}...: {e}")
        return [None, recorded_id]

    try:
        apply_writes(projector_result, recorded_by, recorded_at, db)
    except Exception as e:
        log.error(f"[PROJECTION_FAILED] apply_writes raised for {ref_id[:20]}...: {e}")
        return [None, recorded_id]

    if not projector_result.valid_event:
        log.warning(
            f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... failed projection - NOT marking as valid"
        )
        return [None, recorded_id]

    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (ref_id, recorded_by)
    )

    if event_data and event_data.get('created_at') is not None and event_type:
        safedb.execute(
            """INSERT OR IGNORE INTO projected_events (event_id, event_type, created_at, recorded_by)
               VALUES (?, ?, ?, ?)""",
            (ref_id, event_type, event_data['created_at'], recorded_by)
        )

    unblocked_ids = queues.blocked.notify_event_valid(ref_id, recorded_by, safedb)
    if unblocked_ids:
        project_ids(unblocked_ids, db, _recursion_depth + 1, skip_negentropy=skip_negentropy)
        _cleanup_successfully_projected_events(unblocked_ids, recorded_by, db)

    return [ref_id, recorded_id]


def _chunked(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i:i + size] for i in range(0, len(items), size)]


def _build_key_cache_by_peer(
    key_ids_by_peer: dict[str, set[str]],
    db: Any,
    chunk_size: int,
) -> dict[str, dict[str, Any]]:
    key_cache_by_peer: dict[str, dict[str, Any]] = {}

    for recorded_by, key_ids in key_ids_by_peer.items():
        if not key_ids:
            continue
        safedb = create_safe_db(db, recorded_by=recorded_by)
        key_cache: dict[str, dict[str, Any]] = {}

        for chunk in _chunked(list(key_ids), chunk_size):
            placeholders = ",".join("?" for _ in chunk)
            params = (*chunk, recorded_by)
            rows = safedb.query(
                f"SELECT key_id, key FROM group_keys "
                f"WHERE key_id IN ({placeholders}) AND recorded_by = ?",
                params,
            )
            for row in rows:
                key_cache[row['key_id']] = {
                    'id': crypto.b64decode(row['key_id']),
                    'key': row['key'],
                    'type': 'symmetric',
                }

            params = (*chunk, recorded_by, recorded_by)
            rows = safedb.query(
                f"SELECT prekey_id, private_key FROM group_prekeys "
                f"WHERE prekey_id IN ({placeholders}) AND owner_peer_id = ? AND recorded_by = ?",
                params,
            )
            for row in rows:
                if row['private_key']:
                    key_cache[row['prekey_id']] = {
                        'id': crypto.b64decode(row['prekey_id']),
                        'private_key': row['private_key'],
                        'type': 'asymmetric',
                    }

        if key_cache:
            key_cache_by_peer[recorded_by] = key_cache

    return key_cache_by_peer


def _prefetch_project_contexts(
    recorded_ids: list[str],
    db: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not recorded_ids:
        return [], []

    unsafedb = create_unsafe_db(db)
    chunk_size = int(os.getenv("PROJECT_KEY_CACHE_CHUNK", "400"))

    recorded_meta: dict[str, tuple[str, str, int]] = {}
    for chunk in _chunked(recorded_ids, chunk_size):
        placeholders = ",".join("?" for _ in chunk)
        rows = unsafedb.query(
            f"SELECT id, blob, stored_at FROM store WHERE id IN ({placeholders})",
            tuple(chunk),
        )
        for row in rows:
            try:
                recorded_event = crypto.parse_json(row['blob'])
            except Exception:
                continue
            ref_id = recorded_event.get('ref_id')
            recorded_by = recorded_event.get('recorded_by')
            if ref_id and recorded_by:
                recorded_meta[row['id']] = (recorded_by, ref_id, row['stored_at'])

    if not recorded_meta:
        return [], recorded_ids

    ref_ids = list({ref_id for _recorded_by, ref_id, _recorded_at in recorded_meta.values()})
    event_blobs: dict[str, bytes] = {}
    for chunk in _chunked(ref_ids, chunk_size):
        placeholders = ",".join("?" for _ in chunk)
        rows = unsafedb.query(
            f"SELECT id, blob FROM store WHERE id IN ({placeholders})",
            tuple(chunk),
        )
        for row in rows:
            event_blobs[row['id']] = row['blob']

    key_ids_by_peer: dict[str, set[str]] = {}
    for recorded_id, (recorded_by, ref_id, _recorded_at) in recorded_meta.items():
        event_blob = event_blobs.get(ref_id)
        if not event_blob:
            continue
        if event_blob[:1] in (b'{', b'['):
            raise ValueError("JSON event blobs are no longer supported")
        if len(event_blob) < crypto.KEY_ID_SIZE:
            continue
        key_id_b64 = crypto.b64encode(event_blob[:crypto.KEY_ID_SIZE])
        key_ids_by_peer.setdefault(recorded_by, set()).add(key_id_b64)

    key_cache_by_peer = _build_key_cache_by_peer(key_ids_by_peer, db, chunk_size)

    contexts: list[dict[str, Any]] = []
    fallback_ids: list[str] = []
    for recorded_id in recorded_ids:
        meta = recorded_meta.get(recorded_id)
        if not meta:
            fallback_ids.append(recorded_id)
            continue
        recorded_by, ref_id, recorded_at = meta
        event_blob = event_blobs.get(ref_id)
        if not event_blob:
            contexts.append({
                "recorded_id": recorded_id,
                "ref_id": ref_id,
                "recorded_by": recorded_by,
                "recorded_at": recorded_at,
                "event_blob": None,
                "event_data": None,
                "event_type": None,
                "missing_key_ids": [],
            })
            continue
        peer_cache = key_cache_by_peer.get(recorded_by)
        event_data, event_type, missing_key_ids = _decode_event_blob(
            event_blob, recorded_by, db, peer_cache
        )
        contexts.append({
            "recorded_id": recorded_id,
            "ref_id": ref_id,
            "recorded_by": recorded_by,
            "recorded_at": recorded_at,
            "event_blob": event_blob,
            "event_data": event_data,
            "event_type": event_type,
            "missing_key_ids": missing_key_ids,
        })

    return contexts, fallback_ids


def _decode_event_blob(
    event_blob: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None,
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """Decode wire-format or JSON event payload."""
    event_data = None
    event_type = None
    missing_key_ids: list[str] = []

    if wire_format.is_wire_message_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_message_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_channel_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_channel_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_message_update_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_message_update_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_message_deletion_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_message_deletion_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_message_reaction_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_message_reaction_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_message_reaction_deletion_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_message_reaction_deletion_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_message_attachment_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_message_attachment_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_message_rekey_envelope(event_blob):
        event_data = wire_format.decode_message_rekey_wire_event(event_blob)
    elif wire_format.is_wire_channel_update_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_channel_update_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_group_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_group_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_group_member_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_group_member_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_group_key_envelope(event_blob):
        event_data = wire_format.decode_group_key_wire_event(event_blob)
    elif wire_format.is_wire_group_key_shared_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_group_key_shared_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_group_prekey_envelope(event_blob):
        event_data = wire_format.decode_group_prekey_wire_event(event_blob)
    elif wire_format.is_wire_group_prekey_shared_envelope(event_blob):
        event_data = wire_format.decode_group_prekey_shared_wire_event(event_blob)
    elif wire_format.is_wire_connection_prekey_envelope(event_blob):
        event_data = wire_format.decode_connection_prekey_wire_event(event_blob)
    elif wire_format.is_wire_connection_prekey_shared_envelope(event_blob):
        event_data = wire_format.decode_connection_prekey_shared_wire_event(event_blob)
    elif wire_format.is_wire_connection_request_envelope(event_blob):
        event_data = wire_format.decode_connection_request_wire_event(event_blob)
    elif wire_format.is_wire_connection_ack_envelope(event_blob):
        event_data = wire_format.decode_connection_ack_wire_event(event_blob)
    elif wire_format.is_wire_file_slice(event_blob):
        event_data = wire_format.decode_file_slice_wire_event(event_blob)
    elif wire_format.is_wire_user_envelope(event_blob):
        event_data = wire_format.decode_user_wire_event(event_blob)
    elif wire_format.is_wire_username_update_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_username_update_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_user_removed_envelope(event_blob):
        event_data = wire_format.decode_user_removed_wire_event(event_blob)
    elif wire_format.is_wire_peer_envelope(event_blob):
        event_data = wire_format.decode_peer_wire_event(event_blob)
    elif wire_format.is_wire_peer_shared_envelope(event_blob):
        event_data = wire_format.decode_peer_shared_wire_event(event_blob)
    elif wire_format.is_wire_peer_name_update_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_peer_name_update_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_peer_removed_envelope(event_blob):
        event_data = wire_format.decode_peer_removed_wire_event(event_blob)
    elif wire_format.is_wire_network_envelope(event_blob):
        event_data = wire_format.decode_network_wire_event(event_blob)
    elif wire_format.is_wire_network_name_update_envelope(event_blob):
        event_data, missing_key_ids = wire_format.decode_network_name_update_wire_event(
            event_blob, recorded_by, db, key_cache=key_cache
        )
    elif wire_format.is_wire_admin_envelope(event_blob):
        event_data = wire_format.decode_admin_wire_event(event_blob)
    elif wire_format.is_wire_invite_envelope(event_blob):
        event_data = wire_format.decode_invite_wire_event(event_blob)
    elif wire_format.is_wire_invite_accepted_envelope(event_blob):
        event_data = wire_format.decode_invite_accepted_wire_event(event_blob)
    elif wire_format.is_wire_self_address_envelope(event_blob):
        event_data = wire_format.decode_self_address_wire_event(event_blob)
    elif wire_format.is_wire_observed_address_envelope(event_blob):
        event_data = wire_format.decode_observed_address_wire_event(event_blob)
    elif wire_format.is_wire_network_intro_envelope(event_blob):
        event_data = wire_format.decode_network_intro_wire_event(event_blob)
    elif wire_format.is_wire_negentropy_envelope(event_blob):
        event_data = wire_format.decode_negentropy_wire_event(event_blob)
    else:
        if event_blob and event_blob[:1] in (b'{', b'['):
            raise ValueError("JSON event blobs are no longer supported")

    if isinstance(event_data, dict):
        event_type = event_data.get('type')

    return event_data, event_type, missing_key_ids


def _decode_recorded_context(
    recorded_id: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    unsafedb = create_unsafe_db(db)

    recorded_blob = store.get(recorded_id, unsafedb)
    if not recorded_blob:
        log.warning(f"recorded.project(): blob not found for recorded_id={recorded_id[:30]}...")
        return None

    recorded_event = crypto.parse_json(recorded_blob)
    ref_id = recorded_event['ref_id']
    recorded_by = recorded_event['recorded_by']

    store_row = unsafedb.query_one("SELECT stored_at FROM store WHERE id = ?", (recorded_id,))
    recorded_at = store_row['stored_at'] if store_row else 0

    event_blob = store.get(ref_id, unsafedb)
    if not event_blob:
        return {
            "recorded_id": recorded_id,
            "ref_id": ref_id,
            "recorded_by": recorded_by,
            "recorded_at": recorded_at,
            "event_blob": None,
            "event_data": None,
            "event_type": None,
            "missing_key_ids": [],
        }

    event_data, event_type, missing_key_ids = _decode_event_blob(
        event_blob, recorded_by, db, key_cache
    )

    return {
        "recorded_id": recorded_id,
        "ref_id": ref_id,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "event_blob": event_blob,
        "event_data": event_data,
        "event_type": event_type,
        "missing_key_ids": missing_key_ids,
    }


def _project_with_context(
    ctx: dict[str, Any],
    db: Any,
    _recursion_depth: int,
    skip_negentropy: bool,
    dep_cache: dict[str, Any] | None,
) -> list[str | None]:
    continue_projection, result = _pre_projection_gate(ctx, db, skip_negentropy)
    if not continue_projection:
        return result

    recorded_id = ctx.get("recorded_id")
    ref_id = ctx.get("ref_id")
    recorded_by = ctx.get("recorded_by")
    recorded_at = ctx.get("recorded_at", 0)
    event_data = ctx.get("event_data")
    event_type = event_data.get('type') if isinstance(event_data, dict) else None

    project_pure_fn = registry.get_project_pure_fn(event_type) if event_type else None
    event_spec = registry.get_event_spec(event_type) if event_type else None

    if not (event_spec and project_pure_fn):
        return [None, recorded_id]

    resolve_result = resolver.resolve_event(
        ref_id=ref_id,
        event_type=event_type,
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
        dep_cache=dep_cache,
    )

    if resolve_result.status == 'block':
        missing_deps = list(resolve_result.missing)

        requester_peer_shared_id = event_data.get('peer_shared_id', 'N/A')
        log.warning(
            f"Blocking {event_type} event {ref_id[:20]}... recorded_by={recorded_by[:20]}... "
            f"requester_peer_shared={requester_peer_shared_id[:20]}... missing deps: "
            f"{[d[:20] for d in missing_deps]}"
        )

        safedb = create_safe_db(db, recorded_by=recorded_by)
        from core import queues
        queues.blocked.add(recorded_id, recorded_by, missing_deps, safedb)
        return [None, recorded_id]

    if resolve_result.status == 'reject':
        log.warning(
            f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... rejected: {resolve_result.error}"
        )
        return [None, recorded_id]

    if resolve_result.status != 'ok' or resolve_result.ctx is None:
        log.error(
            f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... resolve returned {resolve_result.status}"
        )
        return [None, recorded_id]

    return _apply_projection(
        ctx,
        resolve_result,
        project_pure_fn,
        db,
        _recursion_depth,
        skip_negentropy=skip_negentropy,
    )

def project_ids(recorded_ids: list[str], db: Any, _recursion_depth: int = 0,
                skip_negentropy: bool = False) -> list[list[str | None]]:
    """Since `recorded` is the event that triggers projection, this is the central function for projection."""
    """It calls the necessary project functions in other modules for the given event types."""

    if _recursion_depth > 100:
        log.error(f"[PROJECT_IDS] RECURSION LIMIT EXCEEDED depth={_recursion_depth} - possible infinite loop!")
        return []

    log.info(f"recorded.project_ids() projecting {len(recorded_ids)} recorded events (depth={_recursion_depth}, skip_neg={skip_negentropy})")
    contexts, fallback_ids = _prefetch_project_contexts(recorded_ids, db)

    dep_cache = resolver.prefetch_dependencies(contexts, db)
    verify_queue: list[tuple[str, bytes, bytes, bytes]] = []
    pending: list[tuple[dict[str, Any], Any, Any]] = []
    projected_ids: list[list[str | None]] = []

    for ctx in contexts:
        try:
            continue_projection, result = _pre_projection_gate(ctx, db, skip_negentropy)
            if not continue_projection:
                projected_ids.append(result)
                continue

            recorded_id = ctx.get("recorded_id")
            ref_id = ctx.get("ref_id")
            recorded_by = ctx.get("recorded_by")
            recorded_at = ctx.get("recorded_at", 0)
            event_data = ctx.get("event_data")
            event_type = event_data.get('type') if isinstance(event_data, dict) else None

            project_pure_fn = registry.get_project_pure_fn(event_type) if event_type else None
            event_spec = registry.get_event_spec(event_type) if event_type else None

            if not (event_spec and project_pure_fn):
                projected_ids.append([None, recorded_id])
                continue

            resolve_result = resolver.resolve_event(
                ref_id=ref_id,
                event_type=event_type,
                event_data=event_data,
                recorded_by=recorded_by,
                recorded_at=recorded_at,
                db=db,
                dep_cache=dep_cache,
                verify_queue=verify_queue,
            )

            if resolve_result.status == 'block':
                missing_deps = list(resolve_result.missing)
                requester_peer_shared_id = event_data.get('peer_shared_id', 'N/A')
                log.warning(
                    f"Blocking {event_type} event {ref_id[:20]}... recorded_by={recorded_by[:20]}... "
                    f"requester_peer_shared={requester_peer_shared_id[:20]}... missing deps: "
                    f"{[d[:20] for d in missing_deps]}"
                )
                safedb = create_safe_db(db, recorded_by=recorded_by)
                from core import queues
                queues.blocked.add(recorded_id, recorded_by, missing_deps, safedb)
                projected_ids.append([None, recorded_id])
                continue

            if resolve_result.status == 'reject':
                log.warning(
                    f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... rejected: {resolve_result.error}"
                )
                projected_ids.append([None, recorded_id])
                continue

            if resolve_result.status != 'ok' or resolve_result.ctx is None:
                log.error(
                    f"[PROJECTION_FAILED] Event {event_type} {ref_id[:20]}... resolve returned {resolve_result.status}"
                )
                projected_ids.append([None, recorded_id])
                continue

            pending.append((ctx, resolve_result, project_pure_fn))
        except Exception as e:
            recorded_id = ctx.get("recorded_id", "")
            log.error(f"[PROJECT_IDS_EXCEPTION] ❌ EXCEPTION projecting recorded_id={recorded_id[:20]}... depth={_recursion_depth}: {str(e)[:200]}")
            import traceback
            traceback.print_exc()
            raise  # Re-raise to fail immediately so we can see the error

    signature_results = _verify_signatures_parallel(verify_queue)
    for ctx, resolve_result, project_pure_fn in pending:
        ref_id = ctx.get("ref_id")
        recorded_id = ctx.get("recorded_id")
        if ref_id in signature_results and not signature_results.get(ref_id):
            log.warning(f"[PROJECTION_FAILED] Event signature invalid for {ref_id[:20]}...")
            projected_ids.append([None, recorded_id])
            continue
        projected_ids.append(
            _apply_projection(
                ctx,
                resolve_result,
                project_pure_fn,
                db,
                _recursion_depth,
                skip_negentropy=skip_negentropy,
            )
        )

    for recorded_id in fallback_ids:
        projected_ids.append(project(recorded_id, db, _recursion_depth, skip_negentropy=skip_negentropy))
    log.info(f"recorded.project_ids() completed projection of {len(recorded_ids)} events (depth={_recursion_depth})")
    return projected_ids


def project(recorded_id: str, db: Any, _recursion_depth: int = 0, _triggered_by: str = 'initial',
            skip_negentropy: bool = False,
            key_cache: dict[str, dict[str, Any]] | None = None) -> list[str | None]:
    """Project recorded event with two-phase dependency checking.

    1. Check encryption keys (block if missing).
    2. Check event dependencies (block if missing).
    Dispatches to type-specific projector if all deps satisfied.

    Args:
        _triggered_by: What triggered this projection (for debugging causality)
        skip_negentropy: If True, skip negentropy bucket updates (caller will batch them)
    """
    ctx = _decode_recorded_context(recorded_id, db, key_cache)
    if ctx is None:
        return [None, None]
    return _project_with_context(ctx, db, _recursion_depth, skip_negentropy, dep_cache=None)


def _cleanup_successfully_projected_events(unblocked_ids: list[str], recorded_by: str, db: Any) -> int:
    """Delete events from blocked_events_ephemeral after successful projection.

    This function implements the cleanup logic that was previously only in test utilities.
    Events with deps_remaining=0 should be removed from the blocked queue AFTER confirming
    they successfully projected (i.e., their ref_id is in valid_events).

    Args:
        unblocked_ids: List of recorded_ids that were just unblocked and re-projected
        recorded_by: The peer who projected them
        db: Database connection

    Returns:
        Number of events cleaned up from blocked_events_ephemeral
    """
    if not unblocked_ids:
        return 0

    from core.db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)
    cleaned_count = 0

    for recorded_id in unblocked_ids:
        try:
            # Get the ref_id from this recorded event
            recorded_blob = store.get(recorded_id, db)
            if not recorded_blob:
                continue

            recorded_data = crypto.parse_json(recorded_blob)
            ref_id = recorded_data.get('ref_id')
            if not ref_id:
                continue

            # Check if the event successfully projected (is in valid_events)
            is_valid = safedb.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
                (ref_id, recorded_by)
            )

            if is_valid:
                # Safe to delete - the event was successfully projected
                safedb.execute(
                    "DELETE FROM blocked_events_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
                    (recorded_id, recorded_by)
                )
                # Also clean up deps table entries
                safedb.execute(
                    "DELETE FROM blocked_event_deps_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
                    (recorded_id, recorded_by)
                )
                cleaned_count += 1
            else:
                # Event projection failed or hasn't completed - keep it blocked for retry
                log.debug(f"_cleanup_successfully_projected_events: event {recorded_id[:20]}... not in valid_events, keeping blocked")
        except Exception as e:
            log.warning(f"_cleanup_successfully_projected_events: error processing {recorded_id[:20]}...: {e}")
            continue

    if cleaned_count > 0:
        log.info(f"_cleanup_successfully_projected_events: cleaned up {cleaned_count}/{len(unblocked_ids)} events from blocked queue")

    return cleaned_count


def create(ref_id: str, recorded_by: str, t_ms: int, db: Any, return_dupes: bool) -> str:
    """Create a recorded event for the given ref_id and return the recorded_id."""

    log.debug(f"recorded.create(): ref_id={ref_id[:20]}..., recorded_by={recorded_by[:20]}...")

    # Build recorded event (no created_by, no created_at - deterministic per peer+event)
    event_data = {
        'type': 'recorded',
        'ref_id': ref_id,
        'recorded_by': recorded_by
    }

    blob = json.dumps(event_data).encode()

    unsafedb = create_unsafe_db(db)

    # Store the recorded blob
    recorded_id = store.blob(blob, t_ms, return_dupes, unsafedb)

    log.debug(f"recorded.create() stored recorded_id={recorded_id}")

    # Projection happens later via explicit project() call
    return recorded_id
