"""Job registry for periodic operations.

Jobs are self-scheduling: each job decides when it should run based on
its own logic. The tick() function asks each job if it should_run() and
executes those that say yes.
"""
from typing import Any, Dict
from abc import ABC, abstractmethod
from db import create_unsafe_db


class Job(ABC):
    """Base class for stateless, deterministic jobs.

    Jobs are pure - they don't maintain state. The tick() function
    passes in last_run_at as a parameter for scheduling decisions.
    """

    def __init__(self, name: str, every_ms: int):
        """Initialize job with name and execution frequency.

        Args:
            name: Unique identifier for this job
            every_ms: Minimum milliseconds between executions
        """
        self.name = name
        self.every_ms = every_ms

    def should_run(self, t_ms: int, last_run_at: int, db: Any) -> bool:
        """Determine if this job should run now.

        Default implementation checks if enough time has elapsed since
        last run. Always runs on first call (when last_run_at == 0).
        Subclasses can override for custom logic.

        Args:
            t_ms: Current time in milliseconds
            last_run_at: Timestamp when job last ran (0 if never)
            db: Database connection

        Returns:
            True if job should run, False otherwise
        """
        # Always run on first tick
        if last_run_at == 0:
            return True
        return t_ms - last_run_at >= self.every_ms

    @abstractmethod
    def run(self, t_ms: int, db: Any) -> Dict[str, Any]:
        """Execute the job.

        Args:
            t_ms: Current time in milliseconds
            db: Database connection

        Returns:
            Dict with execution stats (structure varies by job)
        """
        pass


class SyncSendJob(Job):
    """Send sync requests to all known peers."""

    def __init__(self):
        super().__init__('sync_send', every_ms=5_000)

    def run(self, t_ms: int, db: Any) -> dict:
        from events.transit import sync
        sync.send_request_to_all(t_ms=t_ms, db=db)
        return {}


class SyncReceiveJob(Job):
    """Receive and process incoming sync responses."""

    def __init__(self):
        super().__init__('sync_receive', every_ms=5_000)

    def run(self, t_ms: int, db: Any) -> dict:
        from events.transit import sync
        sync.receive(batch_size=20, t_ms=t_ms, db=db)
        return {}


class MessageRekeyAndPurgeJob(Job):
    """Rekey messages and purge old encryption keys (forward secrecy)."""

    def __init__(self):
        super().__init__('message_rekey_and_purge', every_ms=300_000)

    def run(self, t_ms: int, db: Any) -> dict:
        from events.content import message_deletion
        return message_deletion.run_message_purge_cycle_for_all_peers(t_ms, db)


class PurgeExpiredEventsJob(Job):
    """Purge expired events based on TTL (forward secrecy)."""

    def __init__(self):
        super().__init__('purge_expired_events', every_ms=600_000)

    def run(self, t_ms: int, db: Any) -> dict:
        import purge_expired
        return purge_expired.run_purge_expired_for_all_peers(t_ms, db)


class TransitPrekeyReplenishmentJob(Job):
    """Replenish transit prekeys when running low (smart conditional)."""

    def __init__(self):
        super().__init__('transit_prekey_replenishment', every_ms=3_600_000)

    def should_run(self, t_ms: int, last_run_at: int, db: Any) -> bool:
        """Run if interval elapsed AND at least one peer has low prekeys."""
        # First check time interval
        if not super().should_run(t_ms, last_run_at, db):
            return False

        # Additional check: only run if prekeys actually low
        from events.transit.transit_prekey import MIN_TRANSIT_PREKEYS
        unsafedb = create_unsafe_db(db)

        peers = unsafedb.query("SELECT peer_id FROM local_peers")
        for peer in peers:
            count = unsafedb.query_one(
                "SELECT COUNT(*) as c FROM transit_prekeys WHERE owner_peer_id = ? AND ttl_ms > ?",
                (peer['peer_id'], t_ms)
            )
            if count and count['c'] < MIN_TRANSIT_PREKEYS:
                return True  # At least one peer needs replenishment

        return False  # All peers have enough prekeys

    def run(self, t_ms: int, db: Any) -> dict:
        from events.transit import transit_prekey
        return transit_prekey.replenish_for_all_peers(t_ms, db)


class GroupPrekeyReplenishmentJob(Job):
    """Replenish group prekeys when running low (smart conditional)."""

    def __init__(self):
        super().__init__('group_prekey_replenishment', every_ms=3_600_000)

    def should_run(self, t_ms: int, last_run_at: int, db: Any) -> bool:
        """Run if interval elapsed AND at least one peer has low prekeys."""
        # First check time interval
        if not super().should_run(t_ms, last_run_at, db):
            return False

        # Additional check: only run if prekeys actually low
        from events.group.group_prekey import MIN_GROUP_PREKEYS
        from db import create_safe_db
        unsafedb = create_unsafe_db(db)

        peers = unsafedb.query("SELECT peer_id FROM local_peers")
        for peer in peers:
            safedb = create_safe_db(db, recorded_by=peer['peer_id'])
            count = safedb.query_one(
                "SELECT COUNT(*) as c FROM group_prekeys WHERE recorded_by = ? AND ttl_ms > ?",
                (peer['peer_id'], t_ms)
            )
            if count and count['c'] < MIN_GROUP_PREKEYS:
                return True  # At least one peer needs replenishment

        return False  # All peers have enough prekeys

    def run(self, t_ms: int, db: Any) -> dict:
        from events.group import group_prekey
        return group_prekey.replenish_for_all_peers(t_ms, db)


class SyncConnectSendJob(Job):
    """Send connection announcements to establish/refresh connections."""

    def __init__(self):
        super().__init__('sync_connect_send', every_ms=60_000)  # 1 minute

    def run(self, t_ms: int, db: Any) -> dict:
        from events.transit import sync_connect
        sync_connect.send_connect_to_all(t_ms=t_ms, db=db)
        return {}


class SyncConnectPurgeJob(Job):
    """Purge expired sync connections."""

    def __init__(self):
        super().__init__('sync_connect_purge', every_ms=60_000)  # 1 minute

    def run(self, t_ms: int, db: Any) -> dict:
        from events.transit import sync_connect
        sync_connect.purge_expired(t_ms=t_ms, db=db)
        return {}


# Registry of job instances
JOBS = [
    SyncConnectSendJob(),
    SyncReceiveJob(),
    SyncSendJob(),
    SyncReceiveJob(),  # Run receive again after send
    SyncConnectPurgeJob(),
    MessageRekeyAndPurgeJob(),
    PurgeExpiredEventsJob(),
    TransitPrekeyReplenishmentJob(),
    GroupPrekeyReplenishmentJob(),
]
