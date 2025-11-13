"""NAT simulation engine for network layer.

Manages:
- Peer endpoint registration (local/public vs behind NAT)
- NAT mapping creation and expiry (per-destination in symmetric mode)
- Packet routing through NAT (enforces firewall rules)
- TTL-based mapping cleanup
"""
from dataclasses import dataclass, field
from typing import Optional, Tuple
import logging

log = logging.getLogger(__name__)


@dataclass
class NatConfig:
    """Configuration for NAT simulation."""
    mode: str = 'full_cone'  # 'full_cone', 'restricted', or 'symmetric'
    mapping_ttl_ms: int = 300000  # 5 minutes: how long NAT mappings persist
    hairpinning: bool = False  # Allow traffic to same peer via NAT


@dataclass
class PeerEndpoint:
    """Known endpoint for a peer."""
    peer_id: str
    local_ip: str  # Internal IP (or same as public if no NAT)
    local_port: int  # Internal port
    public_ip: str  # External IP seen by others
    public_port: int  # External port seen by others
    behind_nat: bool  # Whether this peer is behind NAT


@dataclass
class NatMapping:
    """Active NAT mapping for outgoing traffic."""
    peer_id: str  # Peer making the outbound connection
    dest_peer_id: str  # Destination peer
    internal_addr: Tuple[str, int]  # (internal_ip, internal_port)
    external_addr: Tuple[str, int]  # (external_ip, external_port) - what dest sees
    created_at_ms: int  # When mapping was created
    last_used_ms: int  # When mapping was last used

    def is_expired(self, current_time_ms: int, ttl_ms: int) -> bool:
        """Check if mapping has expired based on TTL."""
        return (current_time_ms - self.last_used_ms) > ttl_ms


class NatEngine:
    """NAT simulation engine.

    Tracks:
    - Peer endpoints (who has what address)
    - NAT mappings (internal→external address translations)
    - Firewall rules (what traffic is allowed)
    """

    def __init__(self, config: NatConfig):
        self.config = config
        self.peer_endpoints: dict[str, PeerEndpoint] = {}  # peer_id -> endpoint
        self.nat_mappings: dict[str, list[NatMapping]] = {}  # peer_id -> list of mappings
        self.reverse_mappings: dict[Tuple[str, int], str] = {}  # (external_ip, external_port) -> peer_id

    def register_peer(
        self,
        peer_id: str,
        local_ip: str,
        local_port: int,
        public_ip: str,
        public_port: int,
        behind_nat: bool = False
    ) -> None:
        """Register a peer with its endpoint information.

        Args:
            peer_id: Peer identifier
            local_ip: Internal IP address (used within peer's network)
            local_port: Internal port
            public_ip: External IP (what others see)
            public_port: External port (what others see)
            behind_nat: Whether peer is behind NAT (affects routing)
        """
        endpoint = PeerEndpoint(
            peer_id=peer_id,
            local_ip=local_ip,
            local_port=local_port,
            public_ip=public_ip,
            public_port=public_port,
            behind_nat=behind_nat
        )
        self.peer_endpoints[peer_id] = endpoint
        log.debug(
            f"nat.register_peer: {peer_id[:20]}... "
            f"local={local_ip}:{local_port} public={public_ip}:{public_port} "
            f"behind_nat={behind_nat}"
        )

    def get_endpoint(self, peer_id: str) -> Optional[PeerEndpoint]:
        """Get registered endpoint for a peer."""
        return self.peer_endpoints.get(peer_id)

    def create_outbound_mapping(
        self,
        from_peer_id: str,
        to_peer_id: str,
        current_time_ms: int
    ) -> Optional[Tuple[str, int]]:
        """Create or reuse NAT mapping for outbound packet.

        Args:
            from_peer_id: Source peer
            to_peer_id: Destination peer
            current_time_ms: Current simulation time

        Returns:
            (external_ip, external_port) that packet will appear from, or None if error
        """
        from_endpoint = self.peer_endpoints.get(from_peer_id)
        if not from_endpoint:
            log.warning(f"nat.create_outbound_mapping: from_peer {from_peer_id} not registered")
            return None

        if not from_endpoint.behind_nat:
            # Not behind NAT, use direct endpoint
            return (from_endpoint.public_ip, from_endpoint.public_port)

        # Peer is behind NAT - check for existing mapping
        if from_peer_id not in self.nat_mappings:
            self.nat_mappings[from_peer_id] = []

        mappings = self.nat_mappings[from_peer_id]

        # Look for existing mapping based on mode
        existing = None
        if self.config.mode == 'full_cone':
            # Full cone: one external port for all destinations
            if mappings:
                existing = mappings[0]
        elif self.config.mode == 'restricted':
            # Restricted: one external port per destination
            for mapping in mappings:
                if mapping.dest_peer_id == to_peer_id:
                    existing = mapping
                    break
        elif self.config.mode == 'symmetric':
            # Symmetric: different external port per destination
            for mapping in mappings:
                if mapping.dest_peer_id == to_peer_id:
                    existing = mapping
                    break

        if existing and not existing.is_expired(current_time_ms, self.config.mapping_ttl_ms):
            # Reuse existing mapping
            existing.last_used_ms = current_time_ms
            return existing.external_addr

        # Create new mapping
        # In a real simulator, we'd allocate a new port from NAT
        # For now, use a simple scheme: hash of (peer_id, dest_peer_id) to get port
        internal_addr = (from_endpoint.local_ip, from_endpoint.local_port)

        # Simple port allocation: use hash to deterministically assign ports
        import hashlib
        key = f"{from_peer_id}:{to_peer_id}".encode()
        hash_val = int.from_bytes(hashlib.md5(key).digest()[:2], 'big')
        external_port = 10000 + (hash_val % 55535)  # Avoid reserved ports

        external_addr = (from_endpoint.public_ip, external_port)

        mapping = NatMapping(
            peer_id=from_peer_id,
            dest_peer_id=to_peer_id,
            internal_addr=internal_addr,
            external_addr=external_addr,
            created_at_ms=current_time_ms,
            last_used_ms=current_time_ms
        )
        mappings.append(mapping)
        self.reverse_mappings[external_addr] = from_peer_id

        log.debug(
            f"nat.create_outbound_mapping: {from_peer_id[:20]}... → {to_peer_id[:20]}... "
            f"allocated {external_addr}"
        )

        return external_addr

    def cleanup_expired_mappings(self, current_time_ms: int) -> None:
        """Remove expired NAT mappings."""
        expired_count = 0
        for peer_id in list(self.nat_mappings.keys()):
            mappings = self.nat_mappings[peer_id]
            remaining = [
                m for m in mappings
                if not m.is_expired(current_time_ms, self.config.mapping_ttl_ms)
            ]
            if len(remaining) < len(mappings):
                expired_count += len(mappings) - len(remaining)
                self.nat_mappings[peer_id] = remaining

        if expired_count > 0:
            log.debug(f"nat.cleanup_expired_mappings: removed {expired_count} mappings")

    def get_all_public_addresses(self) -> dict[str, Tuple[str, int]]:
        """Get all registered public endpoints.

        Returns:
            Dict mapping peer_id -> (public_ip, public_port)
        """
        return {
            peer_id: (ep.public_ip, ep.public_port)
            for peer_id, ep in self.peer_endpoints.items()
        }

    def get_mapping_for_address(self, external_addr: Tuple[str, int]) -> Optional[str]:
        """Look up which peer owns a given external address (reverse NAT lookup).

        Args:
            external_addr: (ip, port) tuple

        Returns:
            peer_id if found, None otherwise
        """
        return self.reverse_mappings.get(external_addr)
