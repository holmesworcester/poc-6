"""
Tests for NetworkGateHandler.
"""
import pytest
import sys
from pathlib import Path

# Add project root to path
test_dir = Path(__file__).parent
protocol_dir = test_dir.parent.parent.parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from protocols.quiet.handlers.network_gate import NetworkGateHandler
from protocols.quiet.tests.handlers.test_base import HandlerTestBase


class TestNetworkGateHandler(HandlerTestBase):
    @pytest.mark.unit
    @pytest.mark.handler
    def test_gates_incoming_transit_with_known_mapping(self):
        handler = NetworkGateHandler()
        # Seed mapping
        self.db.execute(
            """
            INSERT OR REPLACE INTO network_gate_transit_index (transit_secret_id, peer_id, network_id, created_at)
            VALUES (?, ?, ?, 0)
            """,
            ("abcd" * 16, "peerA", "net-1"),
        )
        self.db.commit()

        env = self.create_envelope(
            transit_secret_id=("abcd" * 16),
            transit_ciphertext=b"\x00" * 48,
            origin_ip="127.0.0.1",
            origin_port=8080,
            received_at=1,
        )

        assert handler.filter(env)
        out = handler.process(env, self.db)
        assert isinstance(out, list) and len(out) == 1
        result = out[0]
        assert result.get('network_id') == "net-1"

    @pytest.mark.unit
    @pytest.mark.handler
    def test_indexes_outgoing_response_transit(self):
        handler = NetworkGateHandler()
        tsid = "beef" * 16
        env = self.create_envelope(
            is_outgoing=True,
            peer_id="peerA",
            transit_secret_id=tsid,
            network_id="net-2",
            event_blob=b"\x01" + b"\x00"*16 + b"\x00"*24 + b"\x00",
        )

        assert handler.filter(env)
        out = handler.process(env, self.db)
        assert out and out[0].get('transit_secret_id') == tsid

        row = self.db.execute(
            "SELECT network_id, peer_id FROM network_gate_transit_index WHERE transit_secret_id = ?",
            (tsid,),
        ).fetchone()
        assert row is not None
        assert row['network_id'] == "net-2"
        assert row['peer_id'] == "peerA"

    @pytest.mark.unit
    @pytest.mark.handler
    def test_indexes_outgoing_sealed_sync_request_when_hints_present(self):
        handler = NetworkGateHandler()
        tsid = "cafe" * 16
        env = self.create_envelope(
            is_outgoing=True,
            peer_id="peerA",
            event_sealed=b"\xAA\xBB\xCC",
            transit_secret_id=tsid,
            network_id="net-3",
        )

        assert handler.filter(env)
        out = handler.process(env, self.db)
        assert out and out[0].get('transit_secret_id') == tsid
        row = self.db.execute(
            "SELECT network_id FROM network_gate_transit_index WHERE transit_secret_id = ?",
            (tsid,),
        ).fetchone()
        assert row is not None and row['network_id'] == "net-3"
