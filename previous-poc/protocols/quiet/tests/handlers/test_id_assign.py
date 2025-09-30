"""
Tests for ID assignment handler.

Tests the centralized ID assignment logic for all event types.
"""
import hashlib
import json
import pytest
import sqlite3
from pathlib import Path
import tempfile

from protocols.quiet.handlers.id_assign import IdAssignHandler


class TestIdAssignHandler:
    def setup_method(self):
        self.handler = IdAssignHandler()
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        self.db = sqlite3.connect(self.db_path)

    def teardown_method(self):
        self.db.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_ignores_plaintext_only(self):
        """Plaintext-only envelopes are ignored for ID assignment."""
        envelope = {
            'event_plaintext': {
                'type': 'message',
                'content': 'hello world',
                'created_by': 'alice'
            }
        }

        # No event_blob -> filter should be False
        assert self.handler.filter(envelope) == False

        # Direct process call should not assign an event_id
        result = self.handler.process(envelope, self.db)
        processed = result[0]
        assert 'event_id' not in processed

    def test_assigns_id_from_event_blob(self):
        """Test that event_id is assigned from entire event_blob."""
        # Create a sample encrypted blob: header + nonce + ciphertext
        key_id = b'a' * 16
        nonce = b'n' * 24
        ciphertext = b'encrypted_data_here'
        event_blob = b'\x01' + key_id + nonce + ciphertext

        envelope = {
            'event_blob': event_blob,
            'key_secret_id': key_id.hex()
        }

        # Should filter true - needs ID assignment
        assert self.handler.filter(envelope) == True

        # Process should assign event_id from entire blob
        result = self.handler.process(envelope, self.db)
        assert len(result) == 1
        processed = result[0]

        # Check that event_id was assigned from entire blob (hint + ciphertext)
        assert 'event_id' in processed
        expected_id = hashlib.blake2b(event_blob, digest_size=16).hexdigest()
        assert processed['event_id'] == expected_id

    def test_skips_when_id_already_present(self):
        """Test that handler skips envelopes that already have event_id."""
        envelope = {
            'event_id': 'already_has_id',
            'event_plaintext': {'type': 'message', 'content': 'test'}
        }

        # Should not filter - already has ID
        assert self.handler.filter(envelope) == False

    def test_sets_write_to_store_for_local_secrets(self):
        """Test that local secret events get write_to_store=True."""
        for event_type in ['peer_secret', 'key_secret', 'prekey_secret', 'transit_secret']:
            envelope = {
                'event_plaintext': {
                    'type': event_type,
                    'secret': 'test_secret'
                }
            }

            result = self.handler.process(envelope, self.db)
            processed = result[0]

            assert processed.get('write_to_store') == True

    def test_sets_write_to_store_for_events_with_blob(self):
        """Test that events with event_blob get write_to_store=True."""
        envelope = {
            'event_blob': b'some_encrypted_data',
            'event_plaintext': {
                'type': 'message',
                'content': 'test'
            }
        }

        result = self.handler.process(envelope, self.db)
        processed = result[0]

        assert processed.get('write_to_store') == True
        # Should also prioritize blob over plaintext for ID generation
        expected_id = hashlib.blake2b(b'some_encrypted_data', digest_size=16).hexdigest()
        assert processed['event_id'] == expected_id

    def test_sets_write_to_store_for_public_events(self):
        """Test that public events like 'peer' get write_to_store=True."""
        for event_type in ['peer', 'sync_request']:
            envelope = {
                'event_plaintext': {
                    'type': event_type,
                    'data': 'test'
                }
            }

            result = self.handler.process(envelope, self.db)
            processed = result[0]

            assert processed.get('write_to_store') == True

    def test_handles_malformed_blob_gracefully(self):
        """Test that malformed blobs fall back to hashing entire blob."""
        envelope = {
            'event_blob': b'malformed_blob_too_short'
        }

        result = self.handler.process(envelope, self.db)
        processed = result[0]

        # Should still assign an ID, just from entire blob
        assert 'event_id' in processed
        expected_id = hashlib.blake2b(b'malformed_blob_too_short', digest_size=16).hexdigest()
        assert processed['event_id'] == expected_id

    def test_handles_exceptions_gracefully(self):
        """Test that exceptions during processing are caught and logged."""
        envelope = {
            'event_plaintext': {'type': 'message'},  # Valid for filter
            'event_blob': 'invalid_blob_format'  # Will cause exception in blob processing
        }

        # First check that filter passes
        assert self.handler.filter(envelope) == True

        result = self.handler.process(envelope, self.db)
        processed = result[0]

        # Should have either assigned an ID or logged an error
        # Since we have valid plaintext, it should succeed and assign ID
        assert 'event_id' in processed or 'id_assign_error' in processed

    def test_filter_returns_false_for_empty_envelope(self):
        """Test that filter returns False for envelopes with no relevant data."""
        envelope = {}
        assert self.handler.filter(envelope) == False

        envelope = {'some_other_field': 'value'}
        assert self.handler.filter(envelope) == False

    def test_deterministic_id_generation_from_blob(self):
        """Same blobs always generate the same ID."""
        key_id = b'k' * 16
        nonce = b'x' * 24
        ciphertext = b'ct-data'
        blob = b'\x01' + key_id + nonce + ciphertext

        envelope1 = {'event_blob': blob}
        envelope2 = {'event_blob': blob}

        result1 = self.handler.process(envelope1, self.db)
        result2 = self.handler.process(envelope2, self.db)

        assert result1[0]['event_id'] == result2[0]['event_id']
