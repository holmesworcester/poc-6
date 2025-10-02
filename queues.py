"""Queue management for incoming events and blocked event resolution."""
from typing import Any
import json


# ===== Incoming Queue =====

class incoming:
    """Queue for incoming transit blobs."""

    @staticmethod
    def add(blob: bytes, t_ms: int, db: Any) -> None:
        """Add an incoming transit blob to the queue."""
        db.execute("INSERT INTO incoming_blobs (blob, sent_at) VALUES (?, ?)", (blob, t_ms))

    @staticmethod
    def drain(batch_size: int, db: Any) -> list[bytes]:
        """Drain (select and delete) incoming transit blobs up to batch_size."""
        blobs = db.query("SELECT blob FROM incoming_blobs LIMIT ?", (batch_size,))
        db.execute("DELETE FROM incoming_blobs WHERE id IN (SELECT id FROM incoming_blobs LIMIT ?)", (batch_size,))
        return [row['blob'] for row in blobs]


# ===== Blocked Queue =====

class blocked:
    """Queue for events blocked on missing dependencies."""

    @staticmethod
    def add(event_id: str, seen_by_peer_id: str, missing_deps: list[str], db: Any) -> None:
        """Block event_id for seen_by_peer_id until missing_deps are satisfied (blob already in store)."""
        if not missing_deps:
            return

        # Store blocked event
        db.execute(
            """INSERT OR REPLACE INTO blocked_events (event_id, seen_by_peer_id, missing_deps)
               VALUES (?, ?, ?)""",
            (event_id, seen_by_peer_id, json.dumps(missing_deps))
        )

        # Clear and re-insert dependency tracking
        db.execute(
            "DELETE FROM blocked_event_deps WHERE event_id = ? AND seen_by_peer_id = ?",
            (event_id, seen_by_peer_id)
        )

        for dep_id in missing_deps:
            db.execute(
                """INSERT INTO blocked_event_deps (event_id, seen_by_peer_id, dep_id)
                   VALUES (?, ?, ?)""",
                (event_id, seen_by_peer_id, dep_id)
            )

        db.commit()

    @staticmethod
    def process(seen_by_peer_id: str, db: Any) -> list[str]:
        """Unblock events for peer where all deps now satisfied. Returns event_ids to re-project."""
        unblocked = []

        # Get all blocked events for this peer
        blocked_rows = db.query(
            "SELECT event_id FROM blocked_events WHERE seen_by_peer_id = ?",
            (seen_by_peer_id,)
        )

        for row in blocked_rows:
            event_id = row['event_id']

            # Check if all deps are now satisfied
            if blocked._all_deps_satisfied(event_id, seen_by_peer_id, db):
                unblocked.append(event_id)

                # Remove from blocked tables
                db.execute(
                    "DELETE FROM blocked_events WHERE event_id = ? AND seen_by_peer_id = ?",
                    (event_id, seen_by_peer_id)
                )
                # blocked_event_deps will be cascade deleted

        if unblocked:
            db.commit()

        return unblocked

    @staticmethod
    def _all_deps_satisfied(event_id: str, seen_by_peer_id: str, db: Any) -> bool:
        """Check if all dependencies for a blocked event are now satisfied.

        Args:
            event_id: The blocked event to check
            seen_by_peer_id: Which peer's view to check
            db: Database connection

        Returns:
            True if all deps are in valid_events for this peer
        """
        # Get all dependency IDs for this blocked event
        dep_rows = db.query(
            "SELECT dep_id FROM blocked_event_deps WHERE event_id = ? AND seen_by_peer_id = ?",
            (event_id, seen_by_peer_id)
        )

        for dep_row in dep_rows:
            dep_id = dep_row['dep_id']

            # Check if this dep is valid for this peer
            valid = db.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ? LIMIT 1",
                (dep_id, seen_by_peer_id)
            )

            if not valid:
                return False

        return True
