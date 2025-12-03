"""Sync projector - validates sync requests and emits commands.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult with commands
execute_command() - handler that executes bloom comparison and queuing

Design: Option B - projector validates and outputs command, handler executes.
This allows flexibility for different sync protocols (bloom, negentropy, etc.)
without putting protocol-specific logic in the pure projector.

The command contains all data needed for execution:
- bloom filter (for comparison)
- window params (for querying candidate events)
- transit key (for wrapping response)
- requester info (for routing)

The handler (execute_command) does:
- Query events in window
- Apply bloom filter comparison
- Queue missing events for delivery
"""

from typing import TypedDict, NotRequired, Any
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class SyncEventData(TypedDict):
    type: str
    peer_id: str
    signed_by: str
    window_id: int
    window_min: NotRequired[int]
    window_max: NotRequired[int]
    bloom: str  # base64 encoded bloom filter
    response_transit_key_id: str
    response_transit_key: str  # base64 encoded symmetric key
    created_at: int


class SyncInput(TypedDict):
    event_id: str
    event_data: SyncEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict


# ============================================================================
# SPEC
# ============================================================================

SPEC = {
    "encrypted": True,  # Transit-wrapped
    "signer_type": "peer_shared",  # Standard peer signature
    "dependencies": [],  # No blocking deps - handler fetches what it needs
    "tables": [],  # No direct table output - uses commands
}


# ============================================================================
# PROJECTOR - validates and emits command
# ============================================================================

def project(input_dict: SyncInput) -> ProjectorResult:
    """Validate sync request and emit command for execution.

    The pure projector:
    - Validates required fields
    - Signature already verified by resolve() based on SPEC

    The command handler (execute_command) will:
    - Query candidate events in window
    - Apply bloom filter comparison
    - Queue missing events
    """
    event_data: SyncEventData = input_dict["event_data"]

    # Extract fields
    requester_peer_id = event_data.get("peer_id")
    requester_peer_shared_id = event_data.get("signed_by")
    window_id = event_data.get("window_id")
    window_min = event_data.get("window_min")
    window_max = event_data.get("window_max")
    bloom_b64 = event_data.get("bloom")
    response_transit_key_id = event_data.get("response_transit_key_id")
    response_transit_key_b64 = event_data.get("response_transit_key")

    # Validate required fields
    if not all([
        requester_peer_id,
        requester_peer_shared_id,
        window_id is not None,
        bloom_b64,
        response_transit_key_id,
        response_transit_key_b64,
    ]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Emit command for handler to execute
    return ProjectorResult(
        valid=True,
        commands=[{
            "type": "process_sync_request",
            "requester_peer_id": requester_peer_id,
            "requester_peer_shared_id": requester_peer_shared_id,
            "window_id": window_id,
            "window_min": window_min,
            "window_max": window_max,
            "bloom": bloom_b64,
            "response_transit_key_id": response_transit_key_id,
            "response_transit_key": response_transit_key_b64,
        }]
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    peer_id: str = "peer_123",
    signed_by: str = "peer_shared_123",
    window_id: int = 0,
    window_min: int | None = None,
    window_max: int | None = None,
    bloom: str = "AAAA",  # base64 empty bloom
    response_transit_key_id: str = "key_123",
    response_transit_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    data = {
        "type": "sync",
        "peer_id": peer_id,
        "signed_by": signed_by,
        "window_id": window_id,
        "bloom": bloom,
        "response_transit_key_id": response_transit_key_id,
        "response_transit_key": response_transit_key,
        "created_at": created_at,
    }
    if window_min is not None:
        data["window_min"] = window_min
    if window_max is not None:
        data["window_max"] = window_max
    return data


def make_input(
    event_id: str = "sync_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }


# ============================================================================
# COMMAND HANDLER - executes bloom comparison and queuing
# ============================================================================

def execute_command(command: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Execute a sync command (bloom comparison and response queuing).

    This handler receives commands emitted by project() and executes them.
    The pure projector validates; this handler executes the protocol.

    Args:
        command: Command dict with type and parameters
        recorded_by: Peer executing the command (responder)
        recorded_at: Current timestamp
        db: Database connection
    """
    cmd_type = command.get("type")
    if cmd_type != "process_sync_request":
        log.warning(f"sync.execute_command() unknown command type: {cmd_type}")
        return

    _execute_process_sync_request(command, recorded_by, recorded_at, db)


def _execute_process_sync_request(command: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Execute the process_sync_request command (bloom comparison and response).

    Args:
        command: Command with requester info, window params, bloom filter, transit key
        recorded_by: Local peer processing this request (responder)
        recorded_at: Timestamp
        db: Database connection
    """
    import crypto
    import queues
    from db import create_safe_db, create_unsafe_db
    from events.network import sync as sync_module  # For bloom functions
    from events.identity import peer_shared

    # Extract command parameters
    requester_peer_id = command["requester_peer_id"]
    requester_peer_shared_id = command["requester_peer_shared_id"]
    window_id = command["window_id"]
    window_min = command.get("window_min")
    window_max = command.get("window_max")
    bloom_b64 = command["bloom"]
    response_transit_key_id = command["response_transit_key_id"]
    response_transit_key_b64 = command["response_transit_key"]

    log.info(f"sync.execute_command() processing request: window_id={window_id}, range={window_min}-{window_max}")

    # Check if requester is recognized (peer_shared valid OR active connection)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)
    requester_known = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (requester_peer_shared_id, recorded_by)
    )
    if not requester_known:
        conn_ok = unsafedb.query_one(
            "SELECT 1 FROM sync_connections WHERE peer_shared_id = ? AND last_seen_ms + ttl_ms > ?",
            (requester_peer_shared_id, recorded_at)
        )
        if not conn_ok:
            log.debug(f"sync.execute_command() requester={requester_peer_shared_id[:20]}... not recognized")
            return
        log.debug(f"sync.execute_command() ACCEPT via connection: requester={requester_peer_shared_id[:20]}...")

    # Build transit key dict for wrapping responses
    transit_key_id_bytes = crypto.b64decode(response_transit_key_id)
    transit_key_dict = {
        'id': transit_key_id_bytes,
        'key': crypto.b64decode(response_transit_key_b64),
        'type': 'symmetric'
    }

    # Decode bloom filter
    bloom_filter = crypto.b64decode(bloom_b64)

    # Get requester's public key for deriving bloom salt
    requester_public_key = peer_shared.get_public_key(requester_peer_shared_id, recorded_by, db)

    # Query shareable events in window
    MAX_CANDIDATES = 2000
    shareable_rows = safedb.query(
        """SELECT event_id FROM shareable_events
           WHERE can_share_peer_id = ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY RANDOM()
           LIMIT ?""",
        (recorded_by, window_min, window_max, MAX_CANDIDATES)
    )
    log.debug(f"sync.execute_command() found {len(shareable_rows)} candidate events")

    # Derive salt for bloom checking (same salt requester used)
    salt = sync_module.derive_salt(requester_public_key, window_id)

    # Filter events using bloom: send only events that FAIL bloom check
    events_to_send = []
    for row in shareable_rows:
        event_id_str = row['event_id']
        event_id_bytes = crypto.b64decode(event_id_str)

        in_bloom = sync_module.check_bloom(event_id_bytes, bloom_filter, salt)
        if not in_bloom:
            events_to_send.append(event_id_str)
            log.debug(f"sync.execute_command() will_send event={event_id_str[:20]}...")

    log.info(f"sync.execute_command() sending {len(events_to_send)} events to {requester_peer_id[:20]}...")

    # Send filtered events
    for event_id in events_to_send:
        try:
            event_blob = safedb.get_shareable_blob(event_id)
        except Exception as e:
            log.warning(f"sync.execute_command() failed to get blob for {event_id[:20]}...: {e}")
            continue

        # Wrap with transit key and queue
        wrapped_blob = crypto.wrap(event_blob, transit_key_dict, db)
        queues.incoming.add(wrapped_blob, recorded_at, unsafedb)

    # Initialize sync state if needed
    sync_state_exists = unsafedb.query_one(
        "SELECT 1 FROM sync_state_ephemeral WHERE from_peer_id = ? AND to_peer_id = ?",
        (recorded_by, requester_peer_shared_id)
    )
    if not sync_state_exists:
        sync_module.update_sync_state(recorded_by, requester_peer_shared_id, 0, 1, 0, recorded_at, db)
