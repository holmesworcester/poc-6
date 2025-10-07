"""Queue management for incoming events and blocked event resolution."""
from typing import Any
import json
import logging
from db import UnsafeDB, SafeDB

log = logging.getLogger(__name__)


# ===== Incoming Queue =====

class incoming:
    """Queue for incoming transit blobs."""

    @staticmethod
    def add(blob: bytes, t_ms: int, unsafedb: UnsafeDB) -> None:
        """Add an incoming transit blob to the queue."""
        log.debug(f"queues.incoming.add() adding blob size={len(blob)}B, t_ms={t_ms}")
        unsafedb.execute("INSERT INTO incoming_blobs (blob, sent_at) VALUES (?, ?)", (blob, t_ms))

    @staticmethod
    def drain(batch_size: int, unsafedb: UnsafeDB) -> list[bytes]:
        """Drain (select and delete) incoming transit blobs up to batch_size."""
        log.debug(f"queues.incoming.drain() draining up to {batch_size} blobs")
        blobs = unsafedb.query("SELECT blob FROM incoming_blobs LIMIT ?", (batch_size,))
        unsafedb.execute("DELETE FROM incoming_blobs WHERE id IN (SELECT id FROM incoming_blobs LIMIT ?)", (batch_size,))
        result = [row['blob'] for row in blobs]
        log.info(f"queues.incoming.drain() drained {len(result)} blobs")
        return result


# ===== Blocked Queue =====

class blocked:
    """Queue for events blocked on missing dependencies."""

    @staticmethod
    def add(recorded_id: str, recorded_by: str, missing_deps: list[str], safedb: SafeDB) -> None:
        """Block recorded_id for recorded_by until missing_deps are satisfied (blob already in store)."""
        if not missing_deps:
            return

        log.warning(f"queues.blocked.add() blocking recorded_id={recorded_id}, peer={recorded_by}, missing_deps={missing_deps}")

        # Deduplicate dependencies before counting (INSERT OR IGNORE dedupes, so count must match)
        missing_deps_unique = list(set(missing_deps))
        deps_remaining = len(missing_deps_unique)

        # Store blocked event with dependency counter
        safedb.execute(
            """INSERT OR REPLACE INTO blocked_events_ephemeral (recorded_id, recorded_by, missing_deps, deps_remaining)
               VALUES (?, ?, ?, ?)""",
            (recorded_id, recorded_by, json.dumps(missing_deps_unique), deps_remaining)
        )

        # Clear and re-insert dependency tracking
        safedb.execute(
            "DELETE FROM blocked_event_deps_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
            (recorded_id, recorded_by)
        )

        for dep_id in missing_deps_unique:
            safedb.execute(
                """INSERT OR IGNORE INTO blocked_event_deps_ephemeral (recorded_id, recorded_by, dep_id)
                   VALUES (?, ?, ?)""",
                (recorded_id, recorded_by, dep_id)
            )

        safedb.commit()

    @staticmethod
    def process(recorded_by: str, safedb: SafeDB) -> list[str]:
        """Unblock events for peer where all deps now satisfied. Returns recorded_ids to re-project."""
        log.debug(f"queues.blocked.process() checking blocked events for peer={recorded_by}")

        unblocked = []

        # Get all blocked events for this peer
        blocked_rows = safedb.query(
            "SELECT recorded_id FROM blocked_events_ephemeral WHERE recorded_by = ?",
            (recorded_by,)
        )

        log.debug(f"queues.blocked.process() found {len(blocked_rows)} blocked events for peer={recorded_by}")

        for row in blocked_rows:
            recorded_id = row['recorded_id']

            # Check if all deps are now satisfied
            if blocked._all_deps_satisfied(recorded_id, recorded_by, safedb):
                log.info(f"queues.blocked.process() UNBLOCKING recorded_id={recorded_id}, peer={recorded_by}")
                unblocked.append(recorded_id)

                # Remove from blocked tables
                safedb.execute(
                    "DELETE FROM blocked_events_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
                    (recorded_id, recorded_by)
                )
                # blocked_event_deps_ephemeral will be cascade deleted

        if unblocked:
            safedb.commit()
            log.info(f"queues.blocked.process() unblocked {len(unblocked)} events for peer={recorded_by}")

        return unblocked

    @staticmethod
    def _all_deps_satisfied(recorded_id: str, recorded_by: str, safedb: SafeDB) -> bool:
        """Check if all dependencies for a blocked event are now satisfied.

        Args:
            recorded_id: The blocked recorded event to check
            recorded_by: Which peer's view to check
            safedb: SafeDB scoped to recorded_by

        Returns:
            True if all deps are in valid_events for this peer
        """
        # Get all dependency IDs for this blocked event
        dep_rows = safedb.query(
            "SELECT dep_id FROM blocked_event_deps_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
            (recorded_id, recorded_by)
        )

        log.debug(f"queues.blocked._all_deps_satisfied() checking {len(dep_rows)} deps for recorded_id={recorded_id}")

        for dep_row in dep_rows:
            dep_id = dep_row['dep_id']

            # Check if this dep is valid for this peer
            valid = safedb.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
                (dep_id, recorded_by)
            )

            if not valid:
                log.debug(f"queues.blocked._all_deps_satisfied() dep {dep_id} still missing for recorded_id={recorded_id}")
                return False

        log.debug(f"queues.blocked._all_deps_satisfied() ALL deps satisfied for recorded_id={recorded_id}")
        return True

    @staticmethod
    def notify_event_valid(event_id: str, recorded_by: str, safedb: SafeDB) -> list[str]:
        """Notify that an event became valid - decrements counters and unblocks ready events (Kahn's algorithm).

        Args:
            event_id: The event that just became valid
            recorded_by: Which peer recorded this event
            safedb: SafeDB scoped to recorded_by

        Returns:
            List of recorded_ids that were unblocked and need re-projection
        """
        log.debug(f"queues.blocked.notify_event_valid() event_id={event_id}, peer={recorded_by}")

        # Find all events waiting for this dependency (uses idx_blocked_deps_ephemeral_lookup)
        waiting_events = safedb.query("""
            SELECT DISTINCT recorded_id, recorded_by
            FROM blocked_event_deps_ephemeral
            WHERE dep_id = ? AND recorded_by = ?
        """, (event_id, recorded_by))

        if not waiting_events:
            log.debug(f"queues.blocked.notify_event_valid() no events waiting for {event_id}")
            return []

        log.debug(f"queues.blocked.notify_event_valid() found {len(waiting_events)} events waiting for {event_id}")

        # Decrement counters atomically using UPDATE...RETURNING
        # Build placeholders for IN clause
        placeholders = ','.join(['(?, ?)' for _ in waiting_events])
        params = []
        for evt in waiting_events:
            params.extend([evt['recorded_id'], evt['recorded_by']])

        try:
            # Try atomic UPDATE...RETURNING (SQLite 3.35+)
            decremented = safedb.execute_returning(f"""
                UPDATE blocked_events_ephemeral
                SET deps_remaining = deps_remaining - 1
                WHERE (recorded_id, recorded_by) IN (VALUES {placeholders})
                RETURNING recorded_id, deps_remaining
            """, tuple(params))

            # Find which hit zero
            unblocked = [row['recorded_id'] for row in decremented if row['deps_remaining'] == 0]

        except Exception as e:
            # Fallback: manual decrement and check
            log.debug(f"queues.blocked.notify_event_valid() RETURNING failed, using fallback: {e}")
            unblocked = []
            for evt in waiting_events:
                safedb.execute("""
                    UPDATE blocked_events_ephemeral
                    SET deps_remaining = deps_remaining - 1
                    WHERE recorded_id = ? AND recorded_by = ?
                """, (evt['recorded_id'], evt['recorded_by']))

                # Check if it hit zero
                result = safedb.query_one("""
                    SELECT deps_remaining FROM blocked_events_ephemeral
                    WHERE recorded_id = ? AND recorded_by = ?
                """, (evt['recorded_id'], evt['recorded_by']))

                if result and result['deps_remaining'] == 0:
                    unblocked.append(evt['recorded_id'])

        # Cleanup: delete unblocked events from blocked_events_ephemeral
        if unblocked:
            log.info(f"queues.blocked.notify_event_valid() UNBLOCKING {len(unblocked)} events: {unblocked}")

            # Build DELETE with IN clause
            placeholders_del = ','.join(['?' for _ in unblocked])
            safedb.execute(f"""
                DELETE FROM blocked_events_ephemeral
                WHERE recorded_id IN ({placeholders_del}) AND recorded_by = ?
            """, tuple(unblocked) + (recorded_by,))

            safedb.commit()

        return unblocked
