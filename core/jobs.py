"""Job registry for periodic operations.

Jobs are self-scheduling: each job decides when it should run based on
its own logic. The tick() function asks each job if it should_run() and
executes those that say yes.
"""
import logging
from typing import Any, Dict
from abc import ABC, abstractmethod
from .db import create_unsafe_db

log = logging.getLogger(__name__)

# Global frequency multiplier for testing (1.0 = production, 0.01 = 100x faster)
_frequency_multiplier = 1.0

def set_frequency_multiplier(multiplier: float) -> None:
    """Set global frequency multiplier for testing.

    Args:
        multiplier: Multiplier for all job frequencies (e.g., 0.01 for 100x faster)
    """
    global _frequency_multiplier
    _frequency_multiplier = multiplier


def reset_frequency_multiplier() -> None:
    """Reset frequency multiplier to default (for testing cleanup)."""
    global _frequency_multiplier
    _frequency_multiplier = 1.0


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
        # Detect backwards time - this means tick() was called with a timestamp
        # earlier than a previous call, which will cause jobs to silently not run
        if t_ms < last_run_at:
            log.error(f"Job '{self.name}' called with t_ms={t_ms} but last_run_at={last_run_at} - "
                      f"time went backwards by {last_run_at - t_ms}ms! Job will not run.")
            return False
        # Apply frequency multiplier for testing
        effective_interval = int(self.every_ms * _frequency_multiplier)
        return t_ms - last_run_at >= effective_interval

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




class ReceiveJob(Job):
    """Receive and process incoming transit blobs using address-based transport.

    This job:
    1. Transfers packets (loopback for testing, UDP for production)
    2. Drains batch from transport.drain_incoming()
    3. Stores all events via sync.store_incoming()
    4. Projects all recorded events
    """

    def __init__(self):
        super().__init__('receive', every_ms=100)

    def run(self, t_ms: int, db: Any) -> dict:
        from core import transport, receive
        from core import recorded

        # 1. Transfer packets based on configured mode (loopback, simulator, or UDP)
        transport.transfer()

        # 2. Grab batch from incoming (pure - no DB)
        batch = transport.drain_incoming(100)
        if not batch:
            return {'received': 0}

        # 3. Store all (touches DB)
        all_recorded_ids = []
        for blob, from_addr in batch:
            recorded_ids = receive.store_incoming(blob, from_addr, t_ms, db)
            all_recorded_ids.extend(recorded_ids)

        # 4. Project all (touches DB)
        if all_recorded_ids:
            recorded.project_ids(all_recorded_ids, db)

        db.commit()
        return {'received': len(batch)}


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
        from . import purge_expired
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
        from events.network.connection_prekey import MIN_TRANSIT_PREKEYS
        unsafedb = create_unsafe_db(db)

        peers = unsafedb.query("SELECT peer_id FROM local_peers")
        for peer in peers:
            count = unsafedb.query_one(
                "SELECT COUNT(*) as c FROM connection_prekeys WHERE owner_peer_id = ? AND ttl_ms > ?",
                (peer['peer_id'], t_ms)
            )
            if count and count['c'] < MIN_TRANSIT_PREKEYS:
                return True  # At least one peer needs replenishment

        return False  # All peers have enough prekeys

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import connection_prekey
        return connection_prekey.replenish_for_all_peers(t_ms, db)


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
        from .db import create_safe_db
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


class ConnectionSendJob(Job):
    """Send connection requests to establish/refresh connections."""

    def __init__(self):
        super().__init__('connection_send', every_ms=1_000)  # 1 second

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import connection_request
        connection_request.send_to_all(t_ms=t_ms, db=db)
        return {}


class ConnectionPurgeJob(Job):
    """Purge expired connections."""

    def __init__(self):
        super().__init__('connection_purge', every_ms=60_000)  # 1 minute

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import connection_request
        connection_request.purge_expired(t_ms=t_ms, db=db)
        return {}


class SelfAddressAnnounceJob(Job):
    """Announce self-address for all local peers when address changes."""

    def __init__(self):
        super().__init__('self_address_announce', every_ms=60_000)  # 1 minute

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import self_address
        return self_address.announce_for_all_peers(t_ms, db)


class IntroProcessJob(Job):
    """Process pending intro events and trigger hole punching via connection requests.

    When Alice introduces Bob and Charlie:
    - Bob receives intro, sees he needs to connect to Charlie
    - Bob sends connection request to Charlie
    - This sends a packet that creates NAT mapping (hole punch)
    - Charlie does the same
    - Both peers now have bidirectional NAT mappings
    """

    def __init__(self):
        super().__init__('intro_process', every_ms=500)  # Check twice per second

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import intro, connection_request
        from .db import create_safe_db
        import logging

        log = logging.getLogger(__name__)
        unsafedb = create_unsafe_db(db)

        # Get all local peers
        local_peers = unsafedb.query("SELECT peer_id FROM local_peers")
        processed_count = 0

        for peer_row in local_peers:
            peer_id = peer_row['peer_id']
            safedb = create_safe_db(db, recorded_by=peer_id)

            # Get our peer_shared_id for matching
            peer_self = safedb.query_one(
                "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
                (peer_id, peer_id)
            )
            our_peer_shared_id = peer_self['peer_shared_id'] if peer_self else None

            if not our_peer_shared_id:
                continue

            # Get pending intros where we're involved (check both peer1_id and peer2_id)
            # Intros use peer_shared_id or peer_id depending on how they were created
            pending = intro.get_pending_intros(peer_id, db)

            for intro_data in pending:
                peer1_id = intro_data['peer1_id']
                peer2_id = intro_data['peer2_id']

                # Determine if we're peer1 or peer2, and find the other peer
                other_peer_id = None
                if peer1_id == our_peer_shared_id or peer1_id == peer_id:
                    other_peer_id = peer2_id
                elif peer2_id == our_peer_shared_id or peer2_id == peer_id:
                    other_peer_id = peer1_id

                if not other_peer_id:
                    # This intro doesn't involve us, skip
                    continue

                # Try to establish connection to the other peer (hole punch)
                # The other_peer_id might be a peer_shared_id
                log.info(f"IntroProcessJob: {peer_id[:10]}... processing intro to {other_peer_id[:10]}...")

                try:
                    # Send connection request - this creates the NAT mapping
                    connection_request._send_request(
                        to_peer_shared_id=other_peer_id,
                        from_peer_id=peer_id,
                        from_peer_shared_id=our_peer_shared_id,
                        invite_id=None,
                        t_ms=t_ms,
                        db=db
                    )
                except Exception as e:
                    log.warning(f"IntroProcessJob: failed to send to {other_peer_id[:10]}...: {e}")

                # Mark intro as processed regardless of send success
                # (we attempted the hole punch, don't retry endlessly)
                intro.mark_processed(intro_data['intro_id'], peer_id, db)
                processed_count += 1

        return {'processed': processed_count}


class NegentropySyncJob(Job):
    """Send negentropy sync messages to all established connections."""

    def __init__(self):
        super().__init__('negentropy_sync', every_ms=100)  # 100ms for responsive CC

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import negentropy
        return negentropy.sync_all_connections(t_ms=t_ms, db=db)


# Registry of job instances
JOBS = [
    ConnectionSendJob(),
    ReceiveJob(),
    NegentropySyncJob(),
    ConnectionPurgeJob(),
    SelfAddressAnnounceJob(),
    IntroProcessJob(),
    MessageRekeyAndPurgeJob(),
    PurgeExpiredEventsJob(),
    TransitPrekeyReplenishmentJob(),
    GroupPrekeyReplenishmentJob(),
]
