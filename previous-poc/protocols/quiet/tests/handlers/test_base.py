"""
Base test class for handler tests.
"""
import sqlite3
import tempfile
import os
from typing import Dict, Any, List
# Removed core.types import


class HandlerTestBase:
    """Base class for handler tests with common setup."""
    
    def setup_method(self):
        """Set up test database and common fixtures."""
        # Create temporary database
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        
        # Create common tables
        self._create_tables()
        
        # Insert test data
        self._insert_test_data()
    
    def teardown_method(self):
        """Clean up test database."""
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)
    
    def _create_tables(self):
        """Create common tables needed by handlers."""
        # Events table
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_blob BLOB NOT NULL,
                visibility TEXT CHECK(visibility IN ('local-only','network')) DEFAULT 'local-only'
            )
        """)

        # Projected events (used by purge)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS projected_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                projection_data TEXT NOT NULL,
                projected_at INTEGER NOT NULL
            )
        """)
        
        # Network gate transit mapping (handler-owned)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS network_gate_transit_index (
                transit_secret_id TEXT PRIMARY KEY,
                peer_id TEXT NOT NULL,
                network_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        
        # Signing keys table
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS signing_keys (
                peer_id TEXT PRIMARY KEY,
                private_key TEXT NOT NULL
            )
        """)
        
        # Blocked events table (minimal)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_events (
                event_id TEXT PRIMARY KEY,
                envelope_json TEXT NOT NULL,
                missing_deps TEXT NOT NULL
            )
        """)
        
        # Blocked event dependencies
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_event_deps (
                event_id TEXT NOT NULL,
                dep_id TEXT NOT NULL,
                PRIMARY KEY (dep_id, event_id)
            )
        """)
        
        # Deleted events
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS deleted_events (
                event_id TEXT PRIMARY KEY,
                deleted_at INTEGER NOT NULL,
                deleted_by TEXT,
                reason TEXT
            )
        """)

        # Deleted channels
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS deleted_channels (
                channel_id TEXT PRIMARY KEY,
                deleted_at INTEGER NOT NULL,
                deleted_by TEXT
            )
        """)

        # Validated events index for resolve_deps
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS validated_events (
                validated_event_id TEXT PRIMARY KEY
            )
        """)

        # Removed users
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS removed_users (
                user_id TEXT PRIMARY KEY,
                removed_at INTEGER NOT NULL,
                removed_by TEXT
            )
        """)

        self.db.commit()
    
    def _insert_test_data(self):
        """Insert common test data."""
        # No global transit secrets; mappings are managed by network_gate
        
        # Test identity with private key
        self.db.execute("""
            INSERT INTO signing_keys (peer_id, private_key)
            VALUES (?, ?)
        """, ("test_peer_id", "test_private_key"))
        
        # Seed one stored event row
        self.db.execute("""
            INSERT INTO events (event_id, event_blob, visibility)
            VALUES (?, ?, 'network')
        """, ("test_event_id", b"{}"))
        
        self.db.commit()
    
    def create_envelope(self, **kwargs: Any) -> dict[str, Any]:
        """Create a test envelope with given fields."""
        envelope: dict[str, Any] = {}
        envelope.update(kwargs)
        return envelope
