"""Network layer simulator.

Provides:
- Peer registration with address/NAT info
- Packet routing and delivery through NAT
- Address observation (learns public endpoints from origin_ip/port)
- Integration with queues for time-based delivery
"""
from typing import Any, Optional, Tuple
import logging
from core.nat import NatEngine, NatConfig

log = logging.getLogger(__name__)

# Global network simulator instance
_engine: Optional[NatEngine] = None


def init(nat_config: NatConfig = None) -> NatEngine:
    """Initialize the network simulator.

    Args:
        nat_config: NAT configuration (defaults to full_cone with 5min TTL)

    Returns:
        The NAT engine instance
    """
    global _engine

    if nat_config is None:
        nat_config = NatConfig()

    _engine = NatEngine(nat_config)
    log.info(f"network.init: initialized with mode={nat_config.mode}")
    return _engine


def get_engine() -> NatEngine:
    """Get the current network engine, initializing if needed."""
    global _engine
    if _engine is None:
        init()
    return _engine


def register_peer(
    peer_id: str,
    local_ip: str = '127.0.0.1',
    local_port: int = 5000,
    public_ip: str = None,
    public_port: int = None,
    behind_nat: bool = False
) -> None:
    """Register a peer with the network simulator.

    Args:
        peer_id: Peer identifier
        local_ip: Internal IP (defaults to localhost)
        local_port: Internal port (defaults to 5000)
        public_ip: External IP (defaults to same as local if public, random if behind NAT)
        public_port: External port (defaults to same as local if public)
        behind_nat: Whether peer is behind NAT
    """
    engine = get_engine()

    # Default public addresses
    if public_ip is None:
        public_ip = local_ip if not behind_nat else f"203.0.113.{abs(hash(peer_id)) % 256}"
    if public_port is None:
        public_port = local_port

    engine.register_peer(
        peer_id=peer_id,
        local_ip=local_ip,
        local_port=local_port,
        public_ip=public_ip,
        public_port=public_port,
        behind_nat=behind_nat
    )


def send_packet(
    from_peer_id: str,
    to_peer_id: str,
    blob: bytes,
    t_ms: int,
    db: Any
) -> None:
    """Send a packet from one peer to another through the network.

    Creates NAT mappings as needed and enqueues packet for delivery.

    Args:
        from_peer_id: Source peer
        to_peer_id: Destination peer
        blob: Packet data
        t_ms: Current time in milliseconds
        db: Database connection (for enqueueing)
    """
    engine = get_engine()

    # Get destination endpoint
    dest_endpoint = engine.get_endpoint(to_peer_id)
    if not dest_endpoint:
        log.warning(f"network.send_packet: destination {to_peer_id} not registered")
        return

    # Create/reuse NAT mapping for source
    source_addr = engine.create_outbound_mapping(from_peer_id, to_peer_id, t_ms)
    if not source_addr:
        log.warning(f"network.send_packet: could not create mapping for {from_peer_id}")
        return

    # Record observation: we see this packet coming from source_addr
    # This will be used to create address events
    _record_observation(from_peer_id, to_peer_id, source_addr, t_ms, db)

    # Enqueue packet for delivery via network queue
    import queues
    unsafedb = __import__('db').create_unsafe_db(db)

    # Add packet to incoming queue (will be delivered after latency)
    queues.incoming.add(blob, t_ms, unsafedb)
    log.debug(
        f"network.send_packet: {from_peer_id[:20]}... → {to_peer_id[:20]}... "
        f"from {source_addr}, enqueued {len(blob)}B"
    )


def tick(t_ms: int) -> None:
    """Advance network time and clean up expired state.

    Args:
        t_ms: Current simulation time in milliseconds
    """
    engine = get_engine()
    engine.cleanup_expired_mappings(t_ms)
    log.debug(f"network.tick: t_ms={t_ms}")


def _record_observation(
    from_peer_id: str,
    to_peer_id: str,
    observed_addr: Tuple[str, int],
    t_ms: int,
    db: Any
) -> None:
    """Record that we observed a peer's endpoint.

    This will eventually be used to create address events.
    For now, just log it.

    Args:
        from_peer_id: The peer that sent the packet
        to_peer_id: Which peer made this observation
        observed_addr: (ip, port) that the packet appeared from
        t_ms: When the observation occurred
        db: Database connection
    """
    # TODO: Create address event when address event type is ready
    log.debug(
        f"network._record_observation: {to_peer_id[:20]}... observed "
        f"{from_peer_id[:20]}... at {observed_addr}"
    )


def get_all_endpoints() -> dict[str, Tuple[str, int]]:
    """Get all registered peer endpoints.

    Returns:
        Dict mapping peer_id -> (public_ip, public_port)
    """
    engine = get_engine()
    return engine.get_all_public_addresses()


def reset() -> None:
    """Reset network simulator to initial state."""
    global _engine
    _engine = None
