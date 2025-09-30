"""
Tests for receive_from_network handler.
"""
import pytest
from protocols.quiet.handlers.receive_from_network import ReceiveFromNetworkHandler
from protocols.quiet.tests.handlers.test_base import HandlerTestBase


class TestReceiveFromNetworkHandler(HandlerTestBase):
    """Test the receive_from_network handler."""
    
    def setup_method(self):
        """Set up test handler."""
        super().setup_method()
        from protocols.quiet.handlers.receive_from_network import ReceiveFromNetworkHandler
        self.handler = ReceiveFromNetworkHandler()
        self.filter_func = self.handler.filter
        self.handler_func = self.handler.process
        self.handler = ReceiveFromNetworkHandler()
    
    def test_filter_accepts_network_data(self):
        """Test filter accepts envelopes with network data."""
        envelope = self.create_envelope(
            origin_ip="192.168.1.1",
            origin_port=8080,
            received_at=1234567890,
            raw_data=b"test_data"
        )
        assert self.filter_func(envelope) is True
    
    def test_filter_rejects_already_processed(self):
        """Test filter rejects envelopes already processed."""
        envelope = self.create_envelope(
            origin_ip="192.168.1.1",
            origin_port=8080,
            received_at=1234567890,
            raw_data=b"test_data",
            transit_secret_id="already_set"
        )
        assert self.filter_func(envelope) is False
    
    def test_filter_rejects_missing_fields(self):
        """Test filter rejects envelopes missing required fields."""
        # Missing raw_data
        envelope = self.create_envelope(
            origin_ip="192.168.1.1",
            origin_port=8080,
            received_at=1234567890
        )
        assert self.filter_func(envelope) is False
        
        # Missing origin_ip
        envelope = self.create_envelope(
            origin_port=8080,
            received_at=1234567890,
            raw_data=b"test"
        )
        assert self.filter_func(envelope) is False
    
    def test_process_extracts_transit_info(self):
        """Test process extracts transit key ID and ciphertext."""
        # Create raw data: header(0x01) + 16-byte transit_secret_id + ciphertext
        transit_secret_id = b"a" * 16
        transit_ciphertext = b"encrypted_data"
        raw_data = b"\x01" + transit_secret_id + transit_ciphertext
        
        envelope = self.create_envelope(
            origin_ip="192.168.1.1",
            origin_port=8080,
            received_at=1234567890,
            raw_data=raw_data
        )
        
        results = self.handler_func(envelope, self.db)
        
        assert len(results) == 1
        result = results[0]
        assert result['transit_secret_id'] == transit_secret_id.hex()
        assert result['transit_ciphertext'] == transit_ciphertext
        assert result['origin_ip'] == "192.168.1.1"
        assert result['origin_port'] == 8080
        assert result['received_at'] == 1234567890
        assert result['deps'] == [transit_secret_id.hex()]
    
    def test_process_handles_short_data(self):
        """Test process handles data too short for transit layer."""
        envelope = self.create_envelope(
            origin_ip="192.168.1.1",
            origin_port=8080,
            received_at=1234567890,
            raw_data=b"short"  # Less than 33 bytes
        )
        
        results = self.handler_func(envelope, self.db)
        
        assert len(results) == 0  # Should drop the envelope
    
    def test_process_preserves_metadata(self):
        """Test process preserves network metadata."""
        raw_data = b"\x01" + (b"a" * 16) + b"ciphertext"
        
        envelope = self.create_envelope(
            origin_ip="10.0.0.1",
            origin_port=9999,
            received_at=9876543210,
            raw_data=raw_data
        )
        
        results = self.handler_func(envelope, self.db)
        
        assert len(results) == 1
        result = results[0]
        assert result['origin_ip'] == "10.0.0.1"
        assert result['origin_port'] == 9999
        assert result['received_at'] == 9876543210
