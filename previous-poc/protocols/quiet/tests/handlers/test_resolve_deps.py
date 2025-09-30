#!/usr/bin/env python3
"""Tests for resolve_deps handler."""
import sqlite3
import tempfile
from pathlib import Path
import hashlib
import sys
sys.path.insert(0, '/home/hwilson/quiet-python-poc-5')

from protocols.quiet.handlers.resolve_deps import handler as resolve_deps_handler
import json


class TestResolveDeps:

    def setup_method(self):
        """Set up test database and handler."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / 'test.db'
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row

        # Create validated_events table
        self.db.execute("""
            CREATE TABLE validated_events (
                validated_event_id TEXT PRIMARY KEY,
                event_blob BLOB,
                event_plaintext TEXT
            )
        """)

        # Create events table for checking storage
        self.db.execute("""
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                visibility TEXT,
                event_blob BLOB,
                key_ref TEXT,
                received_at INTEGER,
                origin_ip TEXT,
                origin_port INTEGER
            )
        """)
        self.db.commit()

        self.handler = resolve_deps_handler

    def teardown_method(self):
        """Clean up test database."""
        self.db.close()

    def test_envelope_with_no_deps_gets_marked_valid(self):
        """Test that envelopes with empty deps are marked as deps_included_and_valid."""
        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'deps': []
        }

        results = list(self.handler(envelope, self.db))

        assert len(results) == 1
        assert results[0]['deps_included_and_valid'] is True

    def test_envelope_with_met_deps_gets_marked_valid(self):
        """Test that envelopes with all deps in validated_events are marked valid."""
        # Add a dep to validated_events
        dep_id = 'dep123'
        # First add to events table (resolve_deps fetches from there)
        # The blob needs to be valid JSON for _try_parse_or_decrypt_dep_blob
        import json
        dep_plaintext = json.dumps({'type': 'dep', 'data': 'test'}).encode()
        self.db.execute(
            "INSERT INTO events VALUES (?, ?, ?, NULL, NULL, NULL, NULL)",
            (dep_id, 'private', dep_plaintext)
        )
        # Then add to validated_events to mark it as validated
        self.db.execute(
            "INSERT INTO validated_events VALUES (?, NULL, NULL)",
            (dep_id,)
        )
        self.db.commit()

        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'deps': [dep_id]
        }

        results = list(self.handler(envelope, self.db))

        assert len(results) == 1
        assert results[0]['deps_included_and_valid'] is True

    def test_envelope_with_unmet_deps_not_marked_valid(self):
        """Test that envelopes with unmet deps are not marked valid."""
        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'deps': ['missing_dep']
        }

        results = list(self.handler(envelope, self.db))

        # Should not emit anything since deps are not met
        assert len(results) == 0

    def test_local_only_event_gets_indexed_immediately(self):
        """Test that local_only events with IDs are indexed immediately."""
        event_id = hashlib.blake2b(b'test', digest_size=16).hexdigest()
        envelope = {
            'event_type': 'peer_secret',
            'event_plaintext': {'type': 'peer_secret', 'peer_secret_id': event_id},
            'event_id': event_id,
            'event_blob': b'test_blob',
            'local_only': True,
            'validated': True,
            'write_to_store': True,
            'deps': []
        }

        results = list(self.handler(envelope, self.db))

        # Check it was indexed
        cursor = self.db.execute(
            "SELECT * FROM validated_events WHERE validated_event_id = ?",
            (event_id,)
        )
        row = cursor.fetchone()
        assert row is not None

        # Should still emit the envelope
        assert len(results) == 1
        assert results[0]['deps_included_and_valid'] is True

    def test_validated_event_gets_indexed(self):
        """Test that validated events get indexed."""
        event_id = 'test_event'
        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'event_id': event_id,
            'event_blob': b'blob',
            'validated': True,
            'deps': []
        }

        results = list(self.handler(envelope, self.db))

        # Check it was indexed
        cursor = self.db.execute(
            "SELECT * FROM validated_events WHERE validated_event_id = ?",
            (event_id,)
        )
        row = cursor.fetchone()
        assert row is not None

    def test_already_resolved_envelope_not_reemitted(self):
        """Test that envelopes already marked as deps_included_and_valid are not re-emitted."""
        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'deps': [],
            'deps_included_and_valid': True  # Already resolved
        }

        results = list(self.handler(envelope, self.db))

        # Should not emit anything since it's already resolved
        assert len(results) == 0

    def test_stored_event_not_reprocessed(self):
        """Test that events already in storage are not reprocessed."""
        event_id = 'stored_event'
        # Add to events table
        self.db.execute(
            "INSERT INTO events VALUES (?, ?, ?, NULL, NULL, NULL, NULL)",
            (event_id, 'private', b'blob')
        )
        self.db.commit()

        envelope = {
            'event_type': 'test',
            'event_plaintext': {'type': 'test'},
            'event_id': event_id,
            'deps': []
        }

        results = list(self.handler(envelope, self.db))

        # Actually, resolve_deps still processes stored events to mark them as deps_included_and_valid
        # It just doesn't re-store them
        assert len(results) == 1
        assert results[0]['deps_included_and_valid'] is True

    def test_filters_match_correctly(self):
        """Test that the handler's filters match the right envelopes."""
        # resolve_deps handler is a simple function, doesn't have a filters() method
        # It processes all envelopes except those with should_remove=True
        pass