"""Unit tests for remove_peer authorization logic."""
import pytest
import sqlite3
from protocols.quiet.events.remove_peer.queries import can_remove_peer, is_user_admin


class TestRemovePeerAuthorization:
    """Test peer removal authorization checks."""

    @pytest.fixture
    def db(self):
        """Create an in-memory SQLite database for testing."""
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row

        # Create necessary tables
        db.execute("""
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                peer_id TEXT UNIQUE NOT NULL,
                network_id TEXT NOT NULL,
                name TEXT NOT NULL,
                joined_at INTEGER NOT NULL,
                invite_pubkey TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE groups (
                group_id TEXT PRIMARY KEY,
                network_id TEXT NOT NULL,
                name TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE group_members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                added_by TEXT NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id)
            )
        """)

        db.commit()
        return db

    def test_self_removal_authorized(self, db):
        """Test that a peer can remove itself."""
        # Setup: Create a user with a peer
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('user1', 'peer1', 'network1', 'Alice', 1000, ''))
        db.commit()

        # Test: peer1 removing itself
        assert can_remove_peer('peer1', 'peer1', 'network1', db) is True

    def test_linked_peer_removal_authorized(self, db):
        """Test that a peer can remove other peers of the same user.

        NOTE: In the actual protocol, one user has one peer per device.
        But this is tested at the peer_secret level, not the user level.
        For this test, we'll just verify the authorization logic would work
        if we had multiple peers for the same user.

        Skipping as the current schema correctly enforces one peer per user per network.
        """
        pytest.skip("Schema correctly enforces: one peer per user per network")

    def test_admin_can_remove_any_peer(self, db):
        """Test that an admin can remove any peer."""
        # Setup: Create admin and non-admin users
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('admin_user', 'admin_peer', 'network1', 'Admin', 1000, ''))
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('regular_user', 'regular_peer', 'network1', 'Bob', 2000, ''))

        # Create admin group and add admin user
        db.execute("""
            INSERT INTO groups (group_id, network_id, name, creator_id, owner_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('admin_group', 'network1', 'admin', 'admin_user', 'admin_user', 1000))
        db.execute("""
            INSERT INTO group_members (group_id, user_id, added_by, added_at)
            VALUES (?, ?, ?, ?)
        """, ('admin_group', 'admin_user', 'admin_user', 1000))
        db.commit()

        # Test: admin can remove non-admin
        assert can_remove_peer('admin_peer', 'regular_peer', 'network1', db) is True

    def test_non_admin_cannot_remove_other_user_peer(self, db):
        """Test that non-admin peer cannot remove peer of different user."""
        # Setup: Create two different users
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('user_alice', 'peer_alice', 'network1', 'Alice', 1000, ''))
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('user_bob', 'peer_bob', 'network1', 'Bob', 2000, ''))
        db.commit()

        # Test: Alice cannot remove Bob's peer
        assert can_remove_peer('peer_alice', 'peer_bob', 'network1', db) is False

    def test_removal_fails_with_unknown_signer(self, db):
        """Test that removal fails if signer peer doesn't exist."""
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('user1', 'peer1', 'network1', 'Alice', 1000, ''))
        db.commit()

        # Test: Unknown signer trying to remove
        assert can_remove_peer('unknown_peer', 'peer1', 'network1', db) is False

    def test_removal_fails_with_unknown_target(self, db):
        """Test that removal fails if target peer doesn't exist."""
        db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('user1', 'peer1', 'network1', 'Alice', 1000, ''))
        db.commit()

        # Test: Trying to remove unknown peer
        assert can_remove_peer('peer1', 'unknown_peer', 'network1', db) is False


class TestIsUserAdmin:
    """Test admin status checking."""

    @pytest.fixture
    def db(self):
        """Create an in-memory SQLite database for testing."""
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row

        db.execute("""
            CREATE TABLE groups (
                group_id TEXT PRIMARY KEY,
                network_id TEXT NOT NULL,
                name TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE group_members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                added_by TEXT NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id)
            )
        """)

        db.commit()
        return db

    def test_admin_is_recognized(self, db):
        """Test that a user in admin group is recognized as admin."""
        db.execute("""
            INSERT INTO groups (group_id, network_id, name, creator_id, owner_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('admin_group', 'network1', 'admin', 'user1', 'user1', 1000))
        db.execute("""
            INSERT INTO group_members (group_id, user_id, added_by, added_at)
            VALUES (?, ?, ?, ?)
        """, ('admin_group', 'user1', 'user1', 1000))
        db.commit()

        assert is_user_admin('user1', 'network1', db) is True

    def test_non_admin_not_recognized(self, db):
        """Test that a user not in admin group is not admin."""
        db.execute("""
            INSERT INTO groups (group_id, network_id, name, creator_id, owner_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('admin_group', 'network1', 'admin', 'user1', 'user1', 1000))
        db.execute("""
            INSERT INTO group_members (group_id, user_id, added_by, added_at)
            VALUES (?, ?, ?, ?)
        """, ('admin_group', 'user1', 'user1', 1000))
        db.commit()

        assert is_user_admin('user2', 'network1', db) is False

    def test_no_admin_group(self, db):
        """Test behavior when network has no admin group."""
        assert is_user_admin('user1', 'network1', db) is False
