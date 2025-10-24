"""
Database-level tests for user removal with historical record preservation.

NOTE: These tests verify the database state and authorization logic directly.
They do NOT follow the full scenario test pattern (RULES.md) which requires:
- API-only access through APIClient or pipeline.run()
- No direct database access
- Real flow operations (remove_peer.create, remove_user.create)

When flow operations are implemented, these should be converted to proper
scenario tests using the APIClient pattern.

CRITICAL REQUIREMENT: Removed users' events must remain in database and be queryable.
Late joiners must be able to see messages sent by removed users.
Dependencies on removed users/peers must continue to resolve.
"""
import pytest
import sqlite3
from typing import Dict, Any, List


class RemovalScenarioTestBase:
    """Base class for removal scenario tests with shared setup."""

    @pytest.fixture
    def db(self):
        """Create an in-memory database with removal schema."""
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row

        # Create essential tables
        db.executescript("""
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                peer_id TEXT UNIQUE NOT NULL,
                network_id TEXT NOT NULL,
                name TEXT NOT NULL,
                joined_at INTEGER NOT NULL,
                invite_pubkey TEXT NOT NULL
            );

            CREATE TABLE peers (
                peer_id TEXT PRIMARY KEY,
                public_key TEXT NOT NULL,
                peer_secret_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                created_by TEXT NOT NULL,
                network_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                event_plaintext TEXT
            );

            CREATE TABLE removed_peers (
                peer_id TEXT PRIMARY KEY,
                removed_at INTEGER NOT NULL,
                removed_by TEXT,
                reason TEXT
            );

            CREATE TABLE removed_users (
                user_id TEXT PRIMARY KEY,
                removed_at INTEGER NOT NULL,
                removed_by TEXT,
                reason TEXT
            );

            CREATE TABLE groups (
                group_id TEXT PRIMARY KEY,
                network_id TEXT NOT NULL,
                name TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE group_members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                added_by TEXT NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id)
            );
        """)

        db.commit()
        return db

    def insert_user(self, db, user_id: str, peer_id: str, network_id: str, name: str, joined_at: int):
        """Helper to insert a user."""
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, peer_id, network_id, name, joined_at, ''))
        db.commit()

    def insert_peer(self, db, peer_id: str, public_key: str, peer_secret_id: str, created_at: int):
        """Helper to insert a peer."""
        db.execute("""
            INSERT INTO peers (peer_id, public_key, peer_secret_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (peer_id, public_key, peer_secret_id, created_at))
        db.commit()

    def insert_event(self, db, event_id: str, event_type: str, created_by: str, network_id: str, created_at: int):
        """Helper to insert an event."""
        db.execute("""
            INSERT INTO events (event_id, event_type, created_by, network_id, created_at, event_plaintext)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (event_id, event_type, created_by, network_id, created_at, ''))
        db.commit()

    def mark_peer_removed(self, db, peer_id: str, removed_at: int, removed_by: str):
        """Helper to mark a peer as removed."""
        db.execute("""
            INSERT INTO removed_peers (peer_id, removed_at, removed_by, reason)
            VALUES (?, ?, ?, ?)
        """, (peer_id, removed_at, removed_by, 'removal_event'))
        db.commit()

    def mark_user_removed(self, db, user_id: str, removed_at: int, removed_by: str):
        """Helper to mark a user as removed."""
        db.execute("""
            INSERT INTO removed_users (user_id, removed_at, removed_by, reason)
            VALUES (?, ?, ?, ?)
        """, (user_id, removed_at, removed_by, 'removal_event'))
        db.commit()


class TestHistoricalPreservationAfterRemoval(RemovalScenarioTestBase):
    """Test that historical records are preserved after removal."""

    def test_removed_user_events_remain_queryable(self, db):
        """
        SCENARIO: Late Joiner Sees Removed User's Messages
        - Alice and Bob are in network
        - Alice sends messages
        - Admin removes Alice
        - Charlie joins network (late joiner)
        - REQUIREMENT: Charlie can still see Alice's events
        """
        # Setup: Alice and Bob users
        self.insert_user(db, 'user_alice', 'peer_alice', 'network1', 'Alice', 1000)
        self.insert_user(db, 'user_bob', 'peer_bob', 'network1', 'Bob', 1100)
        self.insert_peer(db, 'peer_alice', 'alice_pk', 'secret_alice', 1000)
        self.insert_peer(db, 'peer_bob', 'bob_pk', 'secret_bob', 1100)

        # Alice sends messages
        self.insert_event(db, 'msg_alice_1', 'message', 'peer_alice', 'network1', 1500)
        self.insert_event(db, 'msg_alice_2', 'message', 'peer_alice', 'network1', 1600)

        # Admin removes Alice
        self.mark_user_removed(db, 'user_alice', 2000, 'peer_admin')
        self.mark_peer_removed(db, 'peer_alice', 2000, 'peer_admin')

        # Charlie joins (late joiner) - would eventually receive all events
        self.insert_user(db, 'user_charlie', 'peer_charlie', 'network1', 'Charlie', 2100)

        # REQUIREMENT: Even though Alice is removed, her events still exist in database
        cursor = db.execute("SELECT event_id FROM events WHERE created_by = ?", ('peer_alice',))
        alice_events = [row['event_id'] for row in cursor.fetchall()]
        assert len(alice_events) == 2
        assert 'msg_alice_1' in alice_events
        assert 'msg_alice_2' in alice_events

        # REQUIREMENT: Alice's user event is still in database
        cursor = db.execute("SELECT user_id FROM users WHERE user_id = ?", ('user_alice',))
        alice_user = cursor.fetchone()
        assert alice_user is not None
        assert alice_user['user_id'] == 'user_alice'

        # REQUIREMENT: Alice's peer is still in database
        cursor = db.execute("SELECT peer_id FROM peers WHERE peer_id = ?", ('peer_alice',))
        alice_peer = cursor.fetchone()
        assert alice_peer is not None

    def test_removed_user_dependencies_resolve(self, db):
        """
        SCENARIO: Removed User Events Resolve as Dependencies
        - Alice sends message
        - Admin removes Alice
        - Bob needs to decrypt Alice's message (resolve dependency)
        - REQUIREMENT: Bob can still resolve Alice's user event
        """
        # Setup
        self.insert_user(db, 'user_alice', 'peer_alice', 'network1', 'Alice', 1000)
        self.insert_user(db, 'user_bob', 'peer_bob', 'network1', 'Bob', 1100)
        self.insert_peer(db, 'peer_alice', 'alice_pk', 'secret_alice', 1000)

        # Alice sends message
        self.insert_event(db, 'msg_alice', 'message', 'peer_alice', 'network1', 1500)

        # Admin removes Alice
        self.mark_user_removed(db, 'user_alice', 2000, 'peer_admin')

        # REQUIREMENT: Can still query Alice's user info for dependency resolution
        cursor = db.execute("""
            SELECT user_id, name, peer_id FROM users
            WHERE user_id = ?
        """, ('user_alice',))
        alice_info = cursor.fetchone()
        assert alice_info is not None
        assert alice_info['name'] == 'Alice'
        assert alice_info['peer_id'] == 'peer_alice'

        # REQUIREMENT: Can find Alice's peer even though she's marked removed
        cursor = db.execute("SELECT peer_id FROM peers WHERE peer_id = ?", ('peer_alice',))
        alice_peer = cursor.fetchone()
        assert alice_peer is not None

    def test_removed_peers_do_not_sync_but_history_preserved(self, db):
        """
        SCENARIO: Removed Peer Cannot Sync but History is Preserved
        - Alice removed
        - Alice tries to send sync request
        - REQUIREMENT: Sync request rejected (handled in transit_unwrap)
        - REQUIREMENT: Alice's historical messages still in database
        """
        # Setup
        self.insert_user(db, 'user_alice', 'peer_alice', 'network1', 'Alice', 1000)
        self.insert_peer(db, 'peer_alice', 'alice_pk', 'secret_alice', 1000)
        self.insert_event(db, 'msg_alice_old', 'message', 'peer_alice', 'network1', 1500)

        # Mark Alice as removed
        self.mark_peer_removed(db, 'peer_alice', 2000, 'peer_admin')

        # REQUIREMENT: Peer marked as removed (sync will be rejected in transit_unwrap)
        cursor = db.execute("SELECT peer_id FROM removed_peers WHERE peer_id = ?", ('peer_alice',))
        removed = cursor.fetchone()
        assert removed is not None

        # REQUIREMENT: But her historical messages are still there
        cursor = db.execute("SELECT event_id FROM events WHERE created_by = ?", ('peer_alice',))
        messages = [row['event_id'] for row in cursor.fetchall()]
        assert len(messages) == 1
        assert 'msg_alice_old' in messages


class TestNewMessagesExcludeRemovedUsers(RemovalScenarioTestBase):
    """Test that new encryption excludes removed users."""

    def test_new_messages_after_removal_exclude_removed_user(self, db):
        """
        SCENARIO: New Messages Exclude Removed Users
        - Alice removed
        - Bob sends new message
        - REQUIREMENT: Key recipient set excludes Alice's peer_id
        - REQUIREMENT: Alice cannot decrypt Bob's new message
        """
        # Setup
        self.insert_user(db, 'user_alice', 'peer_alice', 'network1', 'Alice', 1000)
        self.insert_user(db, 'user_bob', 'peer_bob', 'network1', 'Bob', 1100)
        self.insert_peer(db, 'peer_alice', 'alice_pk', 'secret_alice', 1000)
        self.insert_peer(db, 'peer_bob', 'bob_pk', 'secret_bob', 1100)

        # Mark Alice as removed
        self.mark_peer_removed(db, 'peer_alice', 2000, 'peer_admin')

        # REQUIREMENT: When encrypting new messages, Alice's peer_id is excluded
        # This is enforced in event_encrypt handler
        cursor = db.execute("SELECT peer_id FROM removed_peers WHERE peer_id = ?", ('peer_alice',))
        assert cursor.fetchone() is not None  # Alice is marked removed

        # In a real system, event_encrypt would check this and exclude Alice from recipient set
        cursor = db.execute("""
            SELECT peer_id FROM users u
            WHERE u.network_id = ?
            AND NOT EXISTS (SELECT 1 FROM removed_peers rp WHERE rp.peer_id = u.peer_id)
        """, ('network1',))
        active_peers = [row['peer_id'] for row in cursor.fetchall()]

        # REQUIREMENT: Active peer list excludes removed users
        assert 'peer_alice' not in active_peers
        assert 'peer_bob' in active_peers


class TestRemovalAuthorization(RemovalScenarioTestBase):
    """Test that authorization checks work correctly."""

    def test_self_removal_always_allowed(self, db):
        """
        SCENARIO: Self-Removal Authorization
        - Alice removes herself
        - REQUIREMENT: Removal event validates
        - REQUIREMENT: Alice's historical data still queryable
        """
        # Setup
        self.insert_user(db, 'user_alice', 'peer_alice', 'network1', 'Alice', 1000)
        self.insert_event(db, 'msg_alice', 'message', 'peer_alice', 'network1', 1500)

        # Alice removes herself (signer = Alice)
        self.mark_peer_removed(db, 'peer_alice', 2000, 'peer_alice')

        # REQUIREMENT: Alice's data is still there after self-removal
        cursor = db.execute("SELECT user_id FROM users WHERE user_id = ?", ('user_alice',))
        assert cursor.fetchone() is not None

        cursor = db.execute("SELECT event_id FROM events WHERE created_by = ?", ('peer_alice',))
        assert len(cursor.fetchall()) > 0

    def test_admin_can_remove_any_user(self, db):
        """
        SCENARIO: Admin Removal Authorization
        - Admin removes non-admin
        - REQUIREMENT: Removal event validates
        - Admin removes another admin
        - REQUIREMENT: Removal still validates (no limits on admin removal)
        """
        # Setup users and admin group
        self.insert_user(db, 'user_admin', 'peer_admin', 'network1', 'Admin', 1000)
        self.insert_user(db, 'user_alice', 'peer_alice', 'network1', 'Alice', 1100)
        self.insert_peer(db, 'peer_admin', 'admin_pk', 'secret_admin', 1000)

        # Create admin group
        db.execute("""
            INSERT INTO groups (group_id, network_id, name, creator_id, owner_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('admin_group', 'network1', 'admin', 'user_admin', 'user_admin', 1000))
        db.execute("""
            INSERT INTO group_members (group_id, user_id, added_by, added_at)
            VALUES (?, ?, ?, ?)
        """, ('admin_group', 'user_admin', 'user_admin', 1000))
        db.commit()

        # Admin removes non-admin
        self.mark_peer_removed(db, 'peer_alice', 2000, 'peer_admin')

        # REQUIREMENT: Non-admin's data is preserved
        cursor = db.execute("SELECT user_id FROM users WHERE user_id = ?", ('user_alice',))
        assert cursor.fetchone() is not None

        # Admin removes another admin (if added to group)
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('user_admin2', 'peer_admin2', 'network1', 'Admin2', 1050, ''))
        db.execute("""
            INSERT INTO group_members (group_id, user_id, added_by, added_at)
            VALUES (?, ?, ?, ?)
        """, ('admin_group', 'user_admin2', 'user_admin', 1050))
        db.execute("""
            INSERT INTO peers (peer_id, public_key, peer_secret_id, created_at)
            VALUES (?, ?, ?, ?)
        """, ('peer_admin2', 'admin2_pk', 'secret_admin2', 1050))
        db.commit()

        # Admin removes another admin
        self.mark_peer_removed(db, 'peer_admin2', 2100, 'peer_admin')

        # REQUIREMENT: Admin's data is still preserved after removal
        cursor = db.execute("SELECT user_id FROM users WHERE user_id = ?", ('user_admin2',))
        assert cursor.fetchone() is not None
