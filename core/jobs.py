"""Job registry for periodic operations.

Jobs are self-scheduling: each job decides when it should run based on
its own logic. The tick() function asks each job if it should_run() and
executes those that say yes.
"""
import logging
import os
import time
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


def _parse_event_types_env(name: str | None, default_types: list[str]) -> list[str] | None:
    if not name:
        return None
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        types = default_types
    else:
        types = [t.strip() for t in raw.split(",") if t.strip()]
    return types or None


class Job(ABC):
    """Base class for stateless, deterministic jobs.

    Jobs are pure - they don't maintain state. The tick() function
    passes in last_run_at as a parameter for scheduling decisions.
    """

    def __init__(self, name: str, every_ms: int, budget_ms: int | None = None):
        """Initialize job with name, execution frequency, and time budget.

        Args:
            name: Unique identifier for this job
            every_ms: Minimum milliseconds between executions
            budget_ms: Optional time budget for a single run (None/0 = unlimited)
        """
        self.name = name
        self.every_ms = every_ms
        self.budget_ms = budget_ms or 0

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

    def budget_limit_ms(self, override_env: str | None = None) -> int:
        """Return budget in ms for this job, with env override."""
        if override_env:
            raw = os.getenv(override_env)
            if raw is not None and raw.strip() != "":
                return int(raw)
        env_name = "JOB_BUDGET_MS_" + "".join(
            ch if ch.isalnum() else "_" for ch in self.name.upper()
        )
        raw = os.getenv(env_name)
        if raw is not None and raw.strip() != "":
            return int(raw)
        return self.budget_ms

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
    """Receive incoming transport blobs and append to ingest log.

    This job:
    1. Transfers packets (loopback for testing, UDP for production)
    2. Drains batch from transport.drain_incoming()
    3. Unwraps transit in batch (default), then appends to incoming_event_log
    """

    def __init__(self):
        super().__init__('receive', every_ms=50, budget_ms=5)

    def run(self, t_ms: int, db: Any) -> dict:
        from core import transport, ingest

        transport.transfer()

        budget_ms = self.budget_limit_ms()
        chunk_size = int(os.getenv("RECEIVE_INSERT_CHUNK", "1000"))
        batch_size = int(os.getenv("RECEIVE_BATCH", "200"))

        start = time.perf_counter()
        total_received = 0
        total_queued = 0

        while True:
            batch = transport.drain_incoming(batch_size)
            if not batch:
                break
            queued = ingest.queue_incoming(
                batch,
                t_ms,
                db,
                chunk_size=chunk_size,
            )
            total_received += len(batch)
            total_queued += queued
            if budget_ms > 0 and (time.perf_counter() - start) * 1000 >= budget_ms:
                break

        if total_received == 0:
            return {'received': 0, 'queued': 0}

        db.commit()
        return {'received': total_received, 'queued': total_queued}


class SyncRespondJob(Job):
    """Handle incoming negentropy protocol messages quickly."""

    def __init__(self):
        super().__init__('sync_respond', every_ms=50, budget_ms=5)

    def run(self, t_ms: int, db: Any) -> dict:
        from core import ingest, crypto
        from events.network import negentropy

        batch_size = int(os.getenv("SYNC_RESPOND_BATCH", "500"))
        budget_ms = self.budget_limit_ms("SYNC_RESPOND_BUDGET_MS")

        last_log_id = ingest.get_stream_cursor(db, "sync_respond")
        start = time.perf_counter()
        processed = 0
        handled = 0

        while (time.perf_counter() - start) * 1000 < budget_ms:
            rows = db._conn.execute(
                "SELECT id, recorded_by, blob, transit_wrapped "
                "FROM incoming_event_log "
                "WHERE id > ? AND (event_type IS NULL OR event_type != 'negentropy') "
                "ORDER BY id LIMIT ?",
                (last_log_id, batch_size),
            ).fetchall()
            if not rows:
                break

            handled_log_ids: list[tuple[int]] = []
            for log_id, recorded_by, blob, transit_wrapped in rows:
                if transit_wrapped:
                    event_blob, _missing = crypto.unwrap_transit(blob, recorded_by, db)
                else:
                    event_blob = blob
                if not event_blob:
                    continue
                if event_blob[:1] in (b'{', b'['):
                    raise ValueError("JSON event blobs are no longer supported")
                if not negentropy.is_wire_envelope(event_blob):
                    continue
                event_data = negentropy.decode_wire_event(event_blob)
                connection_id = event_data.get("reply_connection_id")
                if not connection_id:
                    continue
                negentropy.handle_incoming(db, recorded_by, connection_id, event_data, t_ms)
                handled += 1
                handled_log_ids.append((log_id,))

            if handled_log_ids:
                db._conn.executemany(
                    "UPDATE incoming_event_log SET event_type = 'negentropy' WHERE id = ?",
                    handled_log_ids,
                )

            processed += len(rows)
            last_log_id = rows[-1][0]
            ingest.set_stream_cursor(db, "sync_respond", last_log_id, t_ms)

        db.commit()
        return {'processed': processed, 'handled': handled}


class SyncUpdateJob(Job):
    """Materialize ingest log into store/recorded/index and add shareable events."""

    def __init__(self):
        super().__init__('sync_update', every_ms=50, budget_ms=50)

    def run(self, t_ms: int, db: Any) -> dict:
        from core import ingest
        batch_size = int(os.getenv("SYNC_UPDATE_BATCH", "8000"))
        max_batch = int(os.getenv("SYNC_UPDATE_BATCH_MAX", str(batch_size)))
        budget_ms = self.budget_limit_ms("SYNC_UPDATE_BUDGET_MS")
        max_budget = int(os.getenv("SYNC_UPDATE_BUDGET_MAX", str(budget_ms)))
        start_log_id = ingest.get_stream_cursor(db, "sync_update")
        log_tail_id = ingest.get_log_tail_id(db)
        backlog = max(0, log_tail_id - start_log_id)
        if backlog > 0 and batch_size > 0:
            scale = min(4.0, backlog / batch_size)
            if scale > 1.0:
                batch_size = min(max_batch, max(batch_size, int(batch_size * scale)))
                if budget_ms > 0:
                    budget_ms = min(max_budget, max(budget_ms, int(budget_ms * scale)))

        processed = 0
        shareable_by_peer: dict[str, list[tuple[str, int | None, int]]] = {}
        start = time.perf_counter()

        while True:
            recorded_ids, max_log_id, _plaintext_recorded_ids, shareable_rows, _protocol_rows = ingest.materialize_log_batch(
                db,
                start_log_id,
                batch_size,
                t_ms,
            )

            if max_log_id <= start_log_id:
                break

            ingest.set_stream_cursor(db, "sync_update", max_log_id, t_ms)
            start_log_id = max_log_id

            if shareable_rows:
                for event_id, recorded_by, recorded_at in shareable_rows:
                    shareable_by_peer.setdefault(recorded_by, []).append((event_id, None, recorded_at))

            processed += len(recorded_ids)

            if budget_ms > 0 and (time.perf_counter() - start) * 1000 >= budget_ms:
                break

        if shareable_by_peer:
            from events.network import negentropy
            for peer_id, events in shareable_by_peer.items():
                negentropy.add_shareable_events_batch(
                    events,
                    peer_id,
                    db,
                    skip_negentropy=False,
                )

        db.commit()
        return {'materialized': processed}


class ProjectionStreamJob(Job):
    """Project recorded events from a named ingest stream."""

    def __init__(
        self,
        name: str,
        stream_name: str,
        every_ms: int,
        batch_env: str,
        default_batch: int,
        budget_ms: int,
        include_env: str | None,
        exclude_env: str | None,
        default_types: list[str] | None,
    ):
        super().__init__(name, every_ms=every_ms, budget_ms=budget_ms)
        self.stream_name = stream_name
        self.batch_env = batch_env
        self.default_batch = default_batch
        self.include_env = include_env
        self.exclude_env = exclude_env
        self.default_types = default_types or []

    def run(self, t_ms: int, db: Any) -> dict:
        from core import projection_streams, recorded

        batch_size = int(os.getenv(self.batch_env, str(self.default_batch)))
        budget_ms = self.budget_limit_ms()

        include_types = _parse_event_types_env(self.include_env, self.default_types)
        exclude_types = _parse_event_types_env(self.exclude_env, self.default_types)

        processed = projection_streams.project_stream(
            db,
            self.stream_name,
            include_types,
            exclude_types,
            batch_size,
            budget_ms,
            t_ms,
            lambda recorded_ids: recorded.project_ids(recorded_ids, db, skip_negentropy=True),
        )

        db.commit()
        return {'projected': processed, 'stream': self.stream_name}


class MessageRekeyAndPurgeJob(Job):
    """Rekey messages and purge old encryption keys (forward secrecy)."""

    def __init__(self):
        super().__init__('message_rekey_and_purge', every_ms=300_000, budget_ms=100)

    def run(self, t_ms: int, db: Any) -> dict:
        from events.content import message_deletion
        return message_deletion.run_message_purge_cycle_for_all_peers(t_ms, db)


class PurgeExpiredEventsJob(Job):
    """Purge expired events based on TTL (forward secrecy)."""

    def __init__(self):
        super().__init__('purge_expired_events', every_ms=600_000, budget_ms=100)

    def run(self, t_ms: int, db: Any) -> dict:
        from . import purge_expired
        return purge_expired.run_purge_expired_for_all_peers(t_ms, db)


class TransitPrekeyReplenishmentJob(Job):
    """Replenish transit prekeys when running low (smart conditional)."""

    def __init__(self):
        super().__init__('transit_prekey_replenishment', every_ms=3_600_000, budget_ms=200)

    def should_run(self, t_ms: int, last_run_at: int, db: Any) -> bool:
        """Run if interval elapsed AND at least one peer has low prekeys."""
        # First check time interval
        if not super().should_run(t_ms, last_run_at, db):
            return False

        # Additional check: only run if pubkeys actually low
        from events.network.connection_pubkey import MIN_CONNECTION_PUBKEYS
        unsafedb = create_unsafe_db(db)

        peers = unsafedb.query("SELECT peer_id FROM local_peers")
        for peer in peers:
            count = unsafedb.query_one(
                "SELECT COUNT(*) as c FROM connection_pubkeys WHERE owner_peer_id = ? AND ttl_ms > ?",
                (peer['peer_id'], t_ms)
            )
            if count and count['c'] < MIN_CONNECTION_PUBKEYS:
                return True  # At least one peer needs replenishment

        return False  # All peers have enough pubkeys

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import connection_pubkey
        return connection_pubkey.replenish_for_all_peers(t_ms, db)


# NOTE: GroupPrekeyReplenishmentJob removed - group_prekey/group_prekey_shared removed
# Sender keys (pubkey/pubkey_shared) are created on join, not replenished periodically


class ConnectionSendJob(Job):
    """Send connection requests to establish/refresh connections."""

    def __init__(self):
        super().__init__('connection_send', every_ms=1_000, budget_ms=5)  # 1 second

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import connection_request
        connection_request.send_to_all(t_ms=t_ms, db=db)
        return {}


class ConnectionPurgeJob(Job):
    """Purge expired connections."""

    def __init__(self):
        super().__init__('connection_purge', every_ms=60_000, budget_ms=25)  # 1 minute

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import connection_request
        connection_request.purge_expired(t_ms=t_ms, db=db)
        return {}


class SelfAddressAnnounceJob(Job):
    """Announce self-address for all local peers when address changes."""

    def __init__(self):
        super().__init__('self_address_announce', every_ms=60_000, budget_ms=25)  # 1 minute

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
        super().__init__('intro_process', every_ms=500, budget_ms=25)  # Check twice per second

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
    """Send negentropy sync messages to all established connections.

    In star topology, negentropy runs slower (10s) since fanout handles
    real-time delivery. Negentropy becomes a background consistency check.
    """

    # Interval in star mode (10 seconds - fanout handles real-time)
    STAR_MODE_INTERVAL_MS = 10_000
    # Interval in mesh mode (1 second - negentropy is primary sync)
    MESH_MODE_INTERVAL_MS = 1_000

    def __init__(self):
        super().__init__('negentropy_sync', every_ms=self.MESH_MODE_INTERVAL_MS, budget_ms=25)

    def should_run(self, t_ms: int, last_run_at: int, db: Any) -> bool:
        """Check if negentropy sync should run.

        Uses slower interval in star mode since fanout handles real-time delivery.
        """
        # Determine effective interval based on network sync mode
        effective_every_ms = self._get_effective_interval(db)
        effective_interval = int(effective_every_ms * _frequency_multiplier)

        # Always run on first tick
        if last_run_at == 0:
            return True

        return t_ms - last_run_at >= effective_interval

    def _get_effective_interval(self, db: Any) -> int:
        """Get sync interval based on network topology.

        Returns slower interval if ANY peer is in star mode.
        """
        from events.identity import network as network_module
        from events.identity import network_settings

        unsafedb = create_unsafe_db(db)
        local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

        for peer_row in local_peers:
            peer_id = peer_row['peer_id']
            network_id = network_module.get_network_id(peer_id, db)
            if network_id:
                sync_mode = network_settings.get_sync_mode(network_id, peer_id, db)
                if sync_mode == 'star':
                    return self.STAR_MODE_INTERVAL_MS

        return self.MESH_MODE_INTERVAL_MS

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import negentropy
        return negentropy.sync_all_connections(t_ms=t_ms, db=db)


class TreeKEMUpdateJob(Job):
    """Periodic TreeKEM updates for forward secrecy using DH-based key exchange.

    This job periodically creates TreeKEM updates for local peers:
    - Uses join delay to let sync catch up before first update
    - Rotates keys every 5 minutes for forward secrecy
    - Builds on current winning base_update_id to avoid conflicts
    - Uses DH-based path secret derivation for O(1) root convergence
    """

    def __init__(self):
        # Run every 5 minutes, but stagger per-peer to avoid thundering herd
        super().__init__('treekem_update', every_ms=300_000, budget_ms=100)

    def run(self, t_ms: int, db: Any) -> dict:
        from events.group import treekem_update
        return treekem_update.update_all_peers_if_needed(t_ms, db)


class TreeKEMReUpdateJob(Job):
    """Re-update superseded members to converge on winning TreeKEM state.

    When concurrent updates occur (same base_update_id), only one wins.
    Superseded members must re-update based on the winner's state to
    ensure everyone converges to the same root secret.

    This job:
    - Runs every 2 seconds to quickly detect superseded updates
    - For each superseded local peer, creates new update based on winner
    - The new update uses DH with winner's pubkeys for convergence
    """

    def __init__(self):
        super().__init__('treekem_re_update', every_ms=2_000, budget_ms=50)

    def run(self, t_ms: int, db: Any) -> dict:
        from events.group import treekem_reupdate
        return treekem_reupdate.reupdate_all_peers(t_ms, db)


HIGH_PRIORITY_DEFAULT_TYPES = [
    # Auth / membership / routing
    "connection_request",
    "connection_ack",
    "connection_pubkey",
    "connection_pubkey_shared",
    "network_intro",
    "self_address",
    "invite",
    "invite_accepted",
    "admin",
    "user_removed",
    "peer_removed",
    "peer",
    "peer_shared",
    "user",
    "network",
    "group",
    "group_member",
    "channel",
    "channel_update",
    # Keys (sender keys)
    "pubkey",
    "pubkey_shared",
    "secret",
    "secret_shared",
    # Messages
    "message",
    "message_update",
    "message_deletion",
    "message_reaction",
    "message_reaction_deletion",
    "message_attachment",
]


# Registry of job instances
JOBS = [
    ConnectionSendJob(),
    ReceiveJob(),
    SyncUpdateJob(),      # Index blobs first
    SyncRespondJob(),     # Then respond to negentropy
    ProjectionStreamJob(
        name="high_priority_project",
        stream_name="high_priority",
        every_ms=50,
        batch_env="HIGH_PRIORITY_BATCH",
        default_batch=200,
        budget_ms=15,
        include_env="HIGH_PRIORITY_TYPES",
        exclude_env=None,
        default_types=HIGH_PRIORITY_DEFAULT_TYPES,
    ),
    ProjectionStreamJob(
        name="low_priority_project",
        stream_name="gc",
        every_ms=100,
        batch_env="LOW_PRIORITY_BATCH",
        default_batch=1000,
        budget_ms=25,
        include_env=None,
        exclude_env="HIGH_PRIORITY_TYPES",
        default_types=HIGH_PRIORITY_DEFAULT_TYPES,
    ),
    NegentropySyncJob(),
    ConnectionPurgeJob(),
    SelfAddressAnnounceJob(),
    IntroProcessJob(),
    MessageRekeyAndPurgeJob(),
    PurgeExpiredEventsJob(),
    TransitPrekeyReplenishmentJob(),
    # NOTE: GroupPrekeyReplenishmentJob removed - group_prekey/group_prekey_shared removed
    # TreeKEM DH-based key exchange jobs
    TreeKEMUpdateJob(),
    TreeKEMReUpdateJob(),
]
