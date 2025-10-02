"""Queue management for incoming events and blocked event resolution."""
from typing import Any
import json
import logging

log = logging.getLogger(__name__)


# ===== Incoming Queue =====

class incoming:
    """Queue for incoming transit blobs."""

    @staticmethod
    def add(blob: bytes, t_ms: int, db: Any) -> None:
        """Add an incoming transit blob to the queue."""
        log.debug(f"queues.incoming.add() adding blob size={len(blob)}B, t_ms={t_ms}")
        db.execute("INSERT INTO incoming_blobs (blob, sent_at) VALUES (?, ?)", (blob, t_ms))

    @staticmethod
    def drain(batch_size: int, db: Any) -> list[bytes]:
        """Drain (select and delete) incoming transit blobs up to batch_size."""
        log.debug(f"queues.incoming.drain() draining up to {batch_size} blobs")
        blobs = db.query("SELECT blob FROM incoming_blobs LIMIT ?", (batch_size,))
        db.execute("DELETE FROM incoming_blobs WHERE id IN (SELECT id FROM incoming_blobs LIMIT ?)", (batch_size,))
        result = [row['blob'] for row in blobs]
        log.info(f"queues.incoming.drain() drained {len(result)} blobs")
        return result


# ===== Blocked Queue =====

class blocked:
    """Queue for events blocked on missing dependencies."""

    @staticmethod
    def add(first_seen_id: str, seen_by_peer_id: str, missing_deps: list[str], db: Any) -> None:
        """Block first_seen_id for seen_by_peer_id until missing_deps are satisfied (blob already in store)."""
        if not missing_deps:
            return

        log.warning(f"queues.blocked.add() blocking first_seen_id={first_seen_id}, peer={seen_by_peer_id}, missing_deps={missing_deps}")

        # Store blocked event
        db.execute(
            """INSERT OR REPLACE INTO blocked_events (first_seen_id, seen_by_peer_id, missing_deps)
               VALUES (?, ?, ?)""",
            (first_seen_id, seen_by_peer_id, json.dumps(missing_deps))
        )

        # Clear and re-insert dependency tracking
        db.execute(
            "DELETE FROM blocked_event_deps WHERE first_seen_id = ? AND seen_by_peer_id = ?",
            (first_seen_id, seen_by_peer_id)
        )

        for dep_id in missing_deps:
            db.execute(
                """INSERT OR IGNORE INTO blocked_event_deps (first_seen_id, seen_by_peer_id, dep_id)
                   VALUES (?, ?, ?)""",
                (first_seen_id, seen_by_peer_id, dep_id)
            )

        db.commit()

    @staticmethod
    def process(seen_by_peer_id: str, db: Any) -> list[str]:
        """Unblock events for peer where all deps now satisfied. Returns first_seen_ids to re-project."""
        log.debug(f"queues.blocked.process() checking blocked events for peer={seen_by_peer_id}")

        unblocked = []

        # Get all blocked events for this peer
        blocked_rows = db.query(
            "SELECT first_seen_id FROM blocked_events WHERE seen_by_peer_id = ?",
            (seen_by_peer_id,)
        )

        log.debug(f"queues.blocked.process() found {len(blocked_rows)} blocked events for peer={seen_by_peer_id}")

        for row in blocked_rows:
            first_seen_id = row['first_seen_id']

            # Check if all deps are now satisfied
            if blocked._all_deps_satisfied(first_seen_id, seen_by_peer_id, db):
                log.info(f"queues.blocked.process() UNBLOCKING first_seen_id={first_seen_id}, peer={seen_by_peer_id}")
                unblocked.append(first_seen_id)

                # Remove from blocked tables
                db.execute(
                    "DELETE FROM blocked_events WHERE first_seen_id = ? AND seen_by_peer_id = ?",
                    (first_seen_id, seen_by_peer_id)
                )
                # blocked_event_deps will be cascade deleted

        if unblocked:
            db.commit()
            log.info(f"queues.blocked.process() unblocked {len(unblocked)} events for peer={seen_by_peer_id}")

        return unblocked

    @staticmethod
    def _all_deps_satisfied(first_seen_id: str, seen_by_peer_id: str, db: Any) -> bool:
        """Check if all dependencies for a blocked event are now satisfied.

        Args:
            first_seen_id: The blocked first_seen event to check
            seen_by_peer_id: Which peer's view to check
            db: Database connection

        Returns:
            True if all deps are in valid_events for this peer
        """
        # Get all dependency IDs for this blocked event
        dep_rows = db.query(
            "SELECT dep_id FROM blocked_event_deps WHERE first_seen_id = ? AND seen_by_peer_id = ?",
            (first_seen_id, seen_by_peer_id)
        )

        log.debug(f"queues.blocked._all_deps_satisfied() checking {len(dep_rows)} deps for first_seen_id={first_seen_id}")

        for dep_row in dep_rows:
            dep_id = dep_row['dep_id']

            # Check if this dep is valid for this peer
            valid = db.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ? LIMIT 1",
                (dep_id, seen_by_peer_id)
            )

            if not valid:
                log.debug(f"queues.blocked._all_deps_satisfied() dep {dep_id} still missing for first_seen_id={first_seen_id}")
                return False

        log.debug(f"queues.blocked._all_deps_satisfied() ALL deps satisfied for first_seen_id={first_seen_id}")
        return True
