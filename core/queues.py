"""Queue management for incoming events and blocked event resolution."""
from typing import Any, Callable, Optional
import json
import logging
import threading
from .db import UnsafeDB, SafeDB
from . import network_config

log = logging.getLogger(__name__)

# Maximum age of packets in queue before bankruptcy (time-based, not count-based)
# This ensures we're never more than a few seconds behind, keeping message latency low
MAX_QUEUE_AGE_MS = 3000  # 3 seconds


# ===== Transport Callback =====
# Thread-local storage for transport callback (allows parallel test execution)
# Signature: (blob: bytes, from_peer: str, to_peer: str, t_ms: int) -> bool
# Returns True if packet was handled (sent via real network), False to use simulator
_thread_local = threading.local()


def set_transport_callback(callback: Optional[Callable[[bytes, str, str, int], bool]]) -> None:
    """Set a transport callback for real network integration.

    When set, the callback is called for each outgoing packet. If it returns True,
    the packet is considered sent via real network and won't go through the simulator.
    If it returns False, the packet goes through the simulator as normal.

    NOTE: This is thread-local, allowing parallel test execution where each test
    thread can have its own transport callback.

    Args:
        callback: Function (blob, from_peer, to_peer, t_ms) -> bool, or None to clear
    """
    _thread_local.transport_callback = callback
    if callback:
        log.info("queues: transport callback set (thread-local)")
    else:
        log.info("queues: transport callback cleared (thread-local)")


def get_transport_callback() -> Optional[Callable[[bytes, str, str, int], bool]]:
    """Get the current transport callback (thread-local)."""
    return getattr(_thread_local, 'transport_callback', None)


# ===== Incoming Queue =====

class incoming:
    """Queue for incoming transit blobs.

    Uses the ns.py-based NetworkSimulator for realistic network simulation
    with proper token bucket bandwidth limiting and pluggable loss models.

    When a transport callback is set (via set_transport_callback), outgoing
    packets are first offered to the callback. If the callback handles the
    packet (returns True), it bypasses the simulator.
    """

    @staticmethod
    def add(blob: bytes, t_ms: int, unsafedb: UnsafeDB, from_peer: str = None, to_peer: str = None,
            source_ip: str = None, source_port: int = None) -> bool:
        """Add an incoming transit blob to the queue with network simulation.

        If a transport callback is set, the packet is first offered to the callback.
        If the callback returns True (handled), the packet bypasses the local queue
        (it was sent over real network to another instance).

        For local delivery (no transport callback or callback returns False):
        - Simulator calculates physics (partitions, NAT, loss, latency)
        - If not dropped, packet is INSERTed into SQLite queue with deliver_at

        Args:
            blob: The packet data
            t_ms: Current simulation time in milliseconds
            unsafedb: Database connection for queue storage
            from_peer: Source peer ID (for partition/NAT checking)
            to_peer: Destination peer ID (for partition/NAT checking)
            source_ip: Source IP address (for address learning)
            source_port: Source port (for address learning)

        Returns:
            True if packet was enqueued/sent, False if dropped
        """
        log.debug(f"queues.incoming.add() adding blob size={len(blob)}B, t_ms={t_ms}")

        # Check if transport callback wants to handle this packet (thread-local)
        callback = get_transport_callback()
        if callback is not None:
            try:
                handled = callback(blob, from_peer or "unknown", to_peer or "unknown", t_ms)
                if handled:
                    log.debug(f"queues.incoming.add() packet handled by transport callback")
                    return True
            except Exception as e:
                log.warning(f"queues.incoming.add() transport callback error: {e}")
                # Fall through to local queue

        sim = network_config.get_simulator()

        # Ensure peers are registered (use defaults if not already registered)
        if from_peer and not sim.nat_engine.get_endpoint(from_peer):
            sim.register_peer(from_peer, behind_nat=False)
        if to_peer and not sim.nat_engine.get_endpoint(to_peer):
            sim.register_peer(to_peer, behind_nat=False)

        # Use simulator for physics calculation (stateless - doesn't store)
        should_drop, deliver_at = sim.calculate_delivery(
            from_peer or "unknown",
            to_peer or "unknown",
            blob,
            t_ms
        )

        if should_drop:
            log.debug(f"queues.incoming.add() dropped blob (simulator physics)")
            return False

        # Store in SQLite queue
        unsafedb.execute(
            "INSERT INTO incoming_blobs (blob, sent_at, deliver_at, source_ip, source_port) VALUES (?, ?, ?, ?, ?)",
            (blob, t_ms, deliver_at, source_ip, source_port)
        )
        log.debug(f"queues.incoming.add() enqueued blob, deliver_at={deliver_at}")
        return True

    @staticmethod
    def drain(batch_size: int, current_time_ms: int, unsafedb: UnsafeDB) -> list[dict]:
        """Drain incoming transit blobs that are ready for delivery.

        Reads from SQLite queue with time-based bankruptcy protection:
        - Drops packets older than MAX_QUEUE_AGE_MS to ensure low latency
        - Returns packets where deliver_at <= current_time_ms

        Args:
            batch_size: Maximum number of packets to return
            current_time_ms: Current simulation time
            unsafedb: Database connection for queue storage

        Returns:
            List of dicts with 'blob', 'source_ip', 'source_port' keys
        """
        log.debug(f"queues.incoming.drain() draining up to {batch_size} blobs at t_ms={current_time_ms}")

        # Time-based bankruptcy: drop packets older than MAX_QUEUE_AGE_MS
        # This ensures we're never more than a few seconds behind
        cutoff_time = current_time_ms - MAX_QUEUE_AGE_MS
        unsafedb.execute(
            "DELETE FROM incoming_blobs WHERE deliver_at < ? AND NOT dropped",
            (cutoff_time,)
        )
        dropped_count = unsafedb.changes()
        if dropped_count > 0:
            log.warning(f"queues.incoming.drain() BANKRUPTCY: dropped {dropped_count} packets older than {MAX_QUEUE_AGE_MS}ms")

        # Try DELETE ... RETURNING (SQLite 3.35+) for atomic drain
        try:
            rows = unsafedb.execute_returning(
                """DELETE FROM incoming_blobs
                   WHERE id IN (
                       SELECT id FROM incoming_blobs
                       WHERE deliver_at <= ? AND NOT dropped
                       ORDER BY deliver_at
                       LIMIT ?
                   )
                   RETURNING blob, source_ip, source_port""",
                (current_time_ms, batch_size)
            )
            result = [{'blob': row['blob'], 'source_ip': row['source_ip'], 'source_port': row['source_port']} for row in rows]
        except Exception as e:
            # Fallback for older SQLite: SELECT then DELETE
            log.debug(f"queues.incoming.drain() RETURNING not supported, using fallback: {e}")
            rows = unsafedb.query(
                """SELECT id, blob, source_ip, source_port FROM incoming_blobs
                   WHERE deliver_at <= ? AND NOT dropped
                   ORDER BY deliver_at
                   LIMIT ?""",
                (current_time_ms, batch_size)
            )

            if not rows:
                log.info(f"queues.incoming.drain() drained 0 blobs")
                return []

            # Delete the rows we're returning
            ids = [row['id'] for row in rows]
            placeholders = ','.join('?' * len(ids))
            unsafedb.execute(
                f"DELETE FROM incoming_blobs WHERE id IN ({placeholders})",
                tuple(ids)
            )
            result = [{'blob': row['blob'], 'source_ip': row['source_ip'], 'source_port': row['source_port']} for row in rows]

        log.info(f"queues.incoming.drain() drained {len(result)} blobs")
        return result

    @staticmethod
    def add_immediate(blob: bytes, t_ms: int, unsafedb: UnsafeDB,
                      source_ip: str = None, source_port: int = None) -> None:
        """Add a packet directly to queue with immediate delivery (no simulation).

        Use this for real networking where packets arrive from external sources
        (UDP, QUIC, WebSocket) and should be processed immediately.

        Args:
            blob: The packet data
            t_ms: Current time in milliseconds (used as both sent_at and deliver_at)
            unsafedb: Database connection for queue storage
            source_ip: Source IP address (for address learning)
            source_port: Source port (for address learning)
        """
        unsafedb.execute(
            "INSERT INTO incoming_blobs (blob, sent_at, deliver_at, source_ip, source_port) VALUES (?, ?, ?, ?, ?)",
            (blob, t_ms, t_ms, source_ip, source_port)  # deliver_at = sent_at for immediate delivery
        )
        log.debug(f"queues.incoming.add_immediate() queued {len(blob)}B for immediate delivery from {source_ip}:{source_port}")

    @staticmethod
    def pending_count(current_time_ms: int, unsafedb: UnsafeDB) -> int:
        """Get count of packets pending delivery.

        Returns the number of packets in the queue that are ready for delivery
        (deliver_at <= current_time_ms) and not dropped.

        Args:
            current_time_ms: Current time in milliseconds
            unsafedb: Database connection for queue storage

        Returns:
            Count of pending packets
        """
        row = unsafedb.query_one(
            "SELECT COUNT(*) as cnt FROM incoming_blobs WHERE deliver_at <= ? AND NOT dropped",
            (current_time_ms,)
        )
        return row['cnt'] if row else 0


# ===== Blocked Queue =====

class blocked:
    """Queue for events blocked on missing dependencies."""

    @staticmethod
    def add(recorded_id: str, recorded_by: str, missing_deps: list[str], safedb: SafeDB) -> None:
        """Block recorded_id for recorded_by until missing_deps are satisfied (blob already in store)."""
        if not missing_deps:
            return

        log.warning(f"queues.blocked.add() blocking recorded_id={recorded_id}, peer={recorded_by}, missing_deps={missing_deps}")

        # Deduplicate dependencies
        missing_deps_unique = list(set(missing_deps))

        # Filter out deps that are ALREADY valid (race condition fix)
        # This handles the case where dep became valid before event was blocked
        actually_missing = []
        for dep_id in missing_deps_unique:
            valid = safedb.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
                (dep_id, recorded_by)
            )
            if not valid:
                actually_missing.append(dep_id)
            else:
                log.info(f"queues.blocked.add() skipping already-valid dep: {dep_id[:20]}...")

        # If all deps are already valid, don't block
        if not actually_missing:
            log.info(f"queues.blocked.add() all deps already valid for recorded_id={recorded_id}, not blocking")
            return

        deps_remaining = len(actually_missing)

        # Store blocked event with dependency counter
        safedb.execute(
            """INSERT OR REPLACE INTO blocked_events_ephemeral (recorded_id, recorded_by, missing_deps, deps_remaining)
               VALUES (?, ?, ?, ?)""",
            (recorded_id, recorded_by, json.dumps(actually_missing), deps_remaining)
        )

        # Clear and re-insert dependency tracking
        safedb.execute(
            "DELETE FROM blocked_event_deps_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
            (recorded_id, recorded_by)
        )

        for dep_id in actually_missing:
            safedb.execute(
                """INSERT OR IGNORE INTO blocked_event_deps_ephemeral (recorded_id, recorded_by, dep_id)
                   VALUES (?, ?, ?)""",
                (recorded_id, recorded_by, dep_id)
            )

        # Note: No commit here - caller owns the transaction (sync entry points or tests)

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
            # Note: No commit here - caller owns the transaction (sync entry points or tests)
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
            # Debug: show what deps ARE being waited for
            all_deps = safedb.query("SELECT DISTINCT dep_id FROM blocked_event_deps_ephemeral WHERE recorded_by = ? LIMIT 5", (recorded_by,))
            log.warning(f"queues.blocked.notify_event_valid() no events waiting for event_id={event_id[:20]}..., peer={recorded_by[:20]}... (other deps being waited for: {[d['dep_id'][:20] for d in all_deps]})")
            return []

        log.debug(f"queues.blocked.notify_event_valid() found {len(waiting_events)} events waiting for {event_id}")

        # ATOMIC FIX: Delete satisfied dependency from deps table BEFORE decrementing counter
        # This keeps counter and table in sync (prevents drift)
        # Build placeholders for IN clause
        placeholders = ','.join(['(?, ?)' for _ in waiting_events])
        params = []
        for evt in waiting_events:
            params.extend([evt['recorded_id'], evt['recorded_by']])

        # Delete the satisfied dependency from deps table (atomic with counter decrement)
        safedb.execute(f"""
            DELETE FROM blocked_event_deps_ephemeral
            WHERE dep_id = ? AND recorded_by = ?
              AND (recorded_id, recorded_by) IN (VALUES {placeholders})
        """, (event_id, recorded_by) + tuple(params))

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

        # IMPORTANT FIX: Do NOT delete unblocked events from blocked_events_ephemeral here!
        # If re-projection fails, the event needs to be re-blocked with its missing deps.
        # Deletion should only happen AFTER confirming successful projection.
        # For now, we keep the event in blocked_events_ephemeral with deps_remaining=0.
        # The convergence test and sync protocol will handle cleaning up truly unblocked events.

        # NOTE: We deliberately do NOT include "all_ready" events (all events with deps_remaining=0).
        # Previously, this code queried for ALL events with deps_remaining=0 and included them
        # in the return value. This caused an exponential cascade during recursive projection:
        # - Event A projected -> unblocks B, C, D + returns ALL_READY (including X, Y, Z)
        # - B projected -> unblocks E + returns ALL_READY (still includes B, C, D, X, Y, Z)
        # - This creates O(n^2) or worse re-processing of events
        #
        # The fix is to only return newly unblocked events. Events that were ready from previous
        # calls will be picked up in subsequent projection passes, not recursively in the current one.

        if unblocked:
            log.info(f"queues.blocked.notify_event_valid() UNBLOCKED {len(unblocked)} events: {unblocked[:3]}")

            # Timeline: Log unblocking event
            from tests.utils import timeline
            timeline.log('unblock', ref_id=event_id, recorded_by=recorded_by,
                         count=len(unblocked), unblocked=unblocked[:3])  # First 3 IDs

        return unblocked

    @staticmethod
    def assert_deps_consistency(recorded_by: str, safedb: SafeDB) -> None:
        """INVARIANT TEST: Verify deps_remaining always equals actual count in deps table.

        This enforces that the counter (deps_remaining) and the source of truth
        (blocked_event_deps_ephemeral table) are always in sync.

        Raises:
            AssertionError if any events have counter != actual deps count
        """
        inconsistent = safedb.query("""
            SELECT
                be.recorded_id,
                be.recorded_by,
                be.deps_remaining as counter,
                COUNT(bed.dep_id) as actual
            FROM blocked_events_ephemeral be
            LEFT JOIN blocked_event_deps_ephemeral bed
                ON be.recorded_id = bed.recorded_id
                AND be.recorded_by = bed.recorded_by
            WHERE be.recorded_by = ?
            GROUP BY be.recorded_id, be.recorded_by
            HAVING counter != actual
        """, (recorded_by,))

        if inconsistent:
            details = []
            for row in inconsistent[:5]:  # Show first 5
                details.append(f"  Event {row['recorded_id'][:20]}...: counter={row['counter']}, actual={row['actual']}")
            raise AssertionError(
                f"Deps counter/table mismatch for peer {recorded_by[:20]}...: "
                f"{len(inconsistent)} events have inconsistent state:\n" + "\n".join(details)
            )
