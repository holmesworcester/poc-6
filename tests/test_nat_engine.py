"""Test NAT engine directly - verify mapping creation and TTL logic."""
import pytest
from core.nat import NatConfig, NatEngine


def test_nat_engine_full_cone_mode():
    """Test NAT full cone mode - single external port per peer."""
    config = NatConfig(mode='full_cone', mapping_ttl_ms=1000)
    engine = NatEngine(config)

    # Register two peers: Alice (public) and Bob (behind NAT)
    engine.register_peer(
        peer_id='alice',
        local_ip='192.168.1.100',
        local_port=5000,
        public_ip='203.0.113.1',
        public_port=5000,
        behind_nat=False
    )

    engine.register_peer(
        peer_id='bob',
        local_ip='192.168.1.50',
        local_port=5000,
        public_ip='203.0.113.2',
        public_port=10000,
        behind_nat=True
    )

    # Bob sends to Alice - should create mapping
    mapping1 = engine.create_outbound_mapping('bob', 'alice', 100)
    assert mapping1 is not None
    assert mapping1[0] == '203.0.113.2'  # Bob's external IP

    # Bob sends to Alice again - should reuse same mapping in full cone
    mapping2 = engine.create_outbound_mapping('bob', 'alice', 200)
    assert mapping2 == mapping1  # Same mapping reused

    print(f"✓ Full cone: same mapping reused ({mapping1})")


def test_nat_engine_symmetric_mode():
    """Test NAT symmetric mode - different port per destination."""
    config = NatConfig(mode='symmetric', mapping_ttl_ms=1000)
    engine = NatEngine(config)

    # Register three peers: Alice (public), Bob and Charlie (behind NAT)
    engine.register_peer(
        peer_id='alice',
        local_ip='192.168.1.100',
        local_port=5000,
        public_ip='203.0.113.1',
        public_port=5000,
        behind_nat=False
    )

    engine.register_peer(
        peer_id='bob',
        local_ip='192.168.1.50',
        local_port=5000,
        public_ip='203.0.113.2',
        public_port=10000,
        behind_nat=True
    )

    engine.register_peer(
        peer_id='charlie',
        local_ip='192.168.1.75',
        local_port=5000,
        public_ip='203.0.113.3',
        public_port=10000,
        behind_nat=True
    )

    # Bob sends to Alice - should create mapping to Alice
    bob_to_alice = engine.create_outbound_mapping('bob', 'alice', 100)
    assert bob_to_alice is not None

    # Bob sends to Charlie - should create DIFFERENT mapping in symmetric mode
    bob_to_charlie = engine.create_outbound_mapping('bob', 'charlie', 200)
    assert bob_to_charlie is not None
    assert bob_to_alice != bob_to_charlie, "Symmetric NAT should use different ports per destination"

    print(f"✓ Symmetric: different mappings for different destinations")
    print(f"  Bob→Alice: {bob_to_alice}")
    print(f"  Bob→Charlie: {bob_to_charlie}")


def test_nat_mapping_ttl_expiry():
    """Test NAT mapping TTL expiry and cleanup."""
    config = NatConfig(mode='full_cone', mapping_ttl_ms=1000)
    engine = NatEngine(config)

    engine.register_peer(
        peer_id='bob',
        local_ip='192.168.1.50',
        local_port=5000,
        public_ip='203.0.113.2',
        public_port=10000,
        behind_nat=True
    )

    engine.register_peer(
        peer_id='alice',
        local_ip='192.168.1.100',
        local_port=5000,
        public_ip='203.0.113.1',
        public_port=5000,
        behind_nat=False
    )

    # Create mapping at t=100
    mapping = engine.create_outbound_mapping('bob', 'alice', 100)
    assert len(engine.nat_mappings.get('bob', [])) == 1

    # At t=1050 (950ms later), mapping still active (TTL=1000ms)
    engine.cleanup_expired_mappings(1050)
    assert len(engine.nat_mappings.get('bob', [])) == 1

    # At t=1101 (1001ms later), mapping should expire (exceeds TTL)
    engine.cleanup_expired_mappings(1101)
    assert len(engine.nat_mappings.get('bob', [])) == 0

    print(f"✓ TTL expiry: mapping created at t=100, expired at t=1101 (TTL=1000ms)")


def test_nat_endpoint_registration():
    """Test peer endpoint registration."""
    engine = NatEngine(NatConfig())

    # Register Alice (public peer)
    engine.register_peer(
        peer_id='alice',
        local_ip='192.168.1.100',
        local_port=5000,
        public_ip='203.0.113.1',
        public_port=5000,
        behind_nat=False
    )

    alice = engine.get_endpoint('alice')
    assert alice is not None
    assert alice.public_ip == '203.0.113.1'
    assert alice.public_port == 5000
    assert alice.behind_nat == False

    # Register Bob (behind NAT)
    engine.register_peer(
        peer_id='bob',
        local_ip='192.168.1.50',
        local_port=5000,
        public_ip='203.0.113.2',
        public_port=10000,
        behind_nat=True
    )

    bob = engine.get_endpoint('bob')
    assert bob is not None
    assert bob.public_ip == '203.0.113.2'
    assert bob.public_port == 10000
    assert bob.behind_nat == True

    # Get all endpoints
    all_endpoints = engine.get_all_public_addresses()
    assert len(all_endpoints) == 2
    assert all_endpoints['alice'] == ('203.0.113.1', 5000)
    assert all_endpoints['bob'] == ('203.0.113.2', 10000)

    print(f"✓ Endpoint registration: {len(all_endpoints)} peers registered")


if __name__ == '__main__':
    test_nat_endpoint_registration()
    test_nat_engine_full_cone_mode()
    test_nat_engine_symmetric_mode()
    test_nat_mapping_ttl_expiry()
    print("\n✅ All NAT tests passed!")
