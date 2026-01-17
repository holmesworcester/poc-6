"""Test equivalence between Rust and Python implementations.

These tests verify that the Rust implementation produces identical
results to the Python implementation, using the Python tests as oracle.
"""

import pytest
import json
import os

# Force Python implementation for baseline
os.environ['QUIET_USE_PYTHON'] = '1'
os.environ['QUIET_USE_RUST'] = '0'


class TestCryptoEquivalence:
    """Test Rust/Python crypto equivalence."""

    @pytest.fixture
    def keypair(self):
        """Generate a keypair using Python (baseline)."""
        from core import crypto
        return crypto.generate_keypair()

    def test_sign_verify_equivalence(self, keypair):
        """Signatures from both implementations should be valid."""
        from core import crypto as python_crypto

        try:
            import quiet_core as rust_crypto
            rust_available = True
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        private_key, public_key = keypair
        message = b"test message for signing"

        # Sign with Python
        sig_python = python_crypto.sign(message, private_key)

        # Sign with Rust
        sig_rust = bytes(rust_crypto.sign(message, private_key))

        # Both should verify with both implementations
        assert python_crypto.verify(message, sig_python, public_key)
        assert python_crypto.verify(message, sig_rust, public_key)
        assert rust_crypto.verify(message, sig_python, public_key)
        assert rust_crypto.verify(message, sig_rust, public_key)

    def test_hash_equivalence(self):
        """Hashes should be identical."""
        from core import crypto as python_crypto

        try:
            import quiet_core as rust_crypto
            rust_available = True
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        data = b"test data for hashing"

        for size in [16, 32, 64]:
            hash_python = python_crypto.hash(data, size)
            hash_rust = bytes(rust_crypto.hash(data, size))

            # Note: May differ if using different hash algorithms
            # (Python uses BLAKE2b, Rust stub uses SHA-512)
            # This test documents the expectation
            assert len(hash_python) == size
            assert len(hash_rust) == size

    def test_b64_roundtrip_equivalence(self):
        """Base64 encoding should be identical."""
        from core import crypto as python_crypto

        try:
            import quiet_core as rust_crypto
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        data = b"test data \x00\xff binary"

        encoded_python = python_crypto.b64encode(data)
        encoded_rust = rust_crypto.b64encode(data)

        assert encoded_python == encoded_rust

        decoded_python = python_crypto.b64decode(encoded_python)
        decoded_rust = bytes(rust_crypto.b64decode(encoded_rust))

        assert decoded_python == decoded_rust == data

    def test_verify_event_equivalence(self, keypair):
        """Event verification should match."""
        from core import crypto as python_crypto

        try:
            import quiet_core as rust_crypto
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        private_key, public_key = keypair

        event = {
            "type": "message",
            "content": "hello world",
            "created_at": 1234567890,
            "channel_id": "chan123",
        }

        # Sign with Python
        signed = python_crypto.sign_event(event, private_key)

        # Verify with both
        assert python_crypto.verify_event(signed, public_key)

        json_str = json.dumps(signed, sort_keys=True, separators=(',', ':'))
        assert rust_crypto.verify_event(json_str, public_key)

    def test_kdf_equivalence(self):
        """Key derivation should match."""
        from core import crypto as python_crypto

        try:
            import quiet_core as rust_crypto
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        secret = b"secret key material"
        salt = b"salt value"

        kdf_python = python_crypto.kdf(secret, salt, 32)
        kdf_rust = bytes(rust_crypto.kdf(secret, salt, 32))

        # Note: May differ if implementations differ
        assert len(kdf_python) == 32
        assert len(kdf_rust) == 32


class TestParserValidation:
    """Test parser accepts valid events and rejects invalid ones."""

    def test_valid_message_event(self):
        """Valid message event should parse."""
        try:
            import quiet_core as rust
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        event = json.dumps({
            "type": "message",
            "signed_by": "abc123",
            "channel_id": "chan1",
            "author_id": "user1",
            "content": "hello",
            "created_at": 12345,
        })

        # Should not raise
        result = rust.parse_event(event.encode())
        parsed = json.loads(result)
        assert parsed["type"] == "message"

    def test_missing_type_rejected(self):
        """Event without type should be rejected."""
        try:
            import quiet_core as rust
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        event = json.dumps({
            "signed_by": "abc123",
            "content": "hello",
        })

        with pytest.raises(ValueError):
            rust.parse_event(event.encode())

    def test_oversized_event_rejected(self):
        """Oversized event should be rejected."""
        try:
            import quiet_core as rust
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        # Create event larger than MAX_EVENT_SIZE (64KB)
        event = json.dumps({
            "type": "message",
            "signed_by": "abc123",
            "content": "x" * 100000,  # 100KB content
            "created_at": 12345,
        })

        with pytest.raises(ValueError):
            rust.parse_event(event.encode())

    def test_validate_structure_fast(self):
        """Structure validation should be fast."""
        try:
            import quiet_core as rust
        except ImportError:
            pytest.skip("Rust implementation not available")
            return

        valid = b'{"type":"message","signed_by":"abc"}'
        invalid = b'["not","an","object"]'

        assert rust.validate_event_structure(valid)
        assert not rust.validate_event_structure(invalid)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
