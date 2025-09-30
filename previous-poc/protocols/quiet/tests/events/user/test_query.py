"""
Tests for user event type query (list).
"""
import pytest
import sqlite3
import tempfile
import os
from protocols.quiet.events.user.queries import get as get_users, get_user, get_user_by_peer_id, count_users, is_user_in_network
from core.db import get_connection, init_database, ReadOnlyConnection


class TestUserQuery:
    """Test user queries."""

    def setup_method(self):
        """Set up test database with sample data."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db = get_connection(self.db_path)

        # Get the protocol directory properly
        import pathlib
        test_dir = pathlib.Path(__file__).parent
        protocol_dir = test_dir.parent.parent.parent
        init_database(self.db, str(protocol_dir))

        # Insert sample user data
        self.db.execute("""
            INSERT INTO users (user_id, peer_id, network_id, name, joined_at, invite_pubkey)
            VALUES
                ('user1', 'peer1', 'network1', 'Alice', 1000, 'invite1'),
                ('user2', 'peer2', 'network1', 'Bob', 2000, 'invite2'),
                ('user3', 'peer3', 'network2', 'Charlie', 3000, 'invite3')
        """)

        # Insert sample group membership data
        self.db.execute("""
            INSERT INTO group_members (group_id, user_id, added_by, added_at)
            VALUES
                ('group1', 'user1', 'user1', 1000),
                ('group1', 'user2', 'user1', 2000),
                ('group2', 'user3', 'user3', 3000)
        """)

        # Ensure peer_locals table exists and insert sample identities for access control
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS peer_locals (
                peer_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                public_key TEXT NOT NULL,
                private_key BLOB NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.db.execute("""
            INSERT INTO peer_locals (peer_id, name, public_key, private_key, created_at)
            VALUES
                ('test_identity', 'Test Identity', 'test_public_key', 'test_private_key', 1000)
        """)

        self.db.commit()

        # Create read-only connection for queries
        self.ro_db = ReadOnlyConnection(self.db)

    def teardown_method(self):
        """Clean up test database."""
        self.db.close()
        os.unlink(self.db_path)

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_users(self):
        """Test getting all users in a network."""
        result = get_users(self.ro_db, {
            'identity_id': 'test_identity',
            'network_id': 'network1'
        })

        assert len(result) == 2
        names = [user['name'] for user in result]
        assert 'Alice' in names
        assert 'Bob' in names

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_users_different_network(self):
        """Test getting users from different network."""
        result = get_users(self.ro_db, {
            'identity_id': 'test_identity',
            'network_id': 'network2'
        })

        assert len(result) == 1
        assert result[0]['name'] == 'Charlie'

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_users_requires_identity(self):
        """Test that get_users requires identity_id."""
        with pytest.raises(ValueError, match="identity_id is required"):
            get_users(self.ro_db, {'network_id': 'network1'})

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_users_requires_network(self):
        """Test that get_users requires network_id."""
        with pytest.raises(ValueError, match="network_id is required"):
            get_users(self.ro_db, {'identity_id': 'test_identity'})

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_user_by_id(self):
        """Test getting a specific user by ID."""
        result = get_user(self.ro_db, {'user_id': 'user1'})

        assert result is not None
        assert result['name'] == 'Alice'
        assert result['peer_id'] == 'peer1'
        assert result['network_id'] == 'network1'

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_user_not_found(self):
        """Test getting a non-existent user."""
        result = get_user(self.ro_db, {'user_id': 'nonexistent'})

        assert result is None

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_user_by_peer_id(self):
        """Test getting user by peer ID."""
        result = get_user_by_peer_id(self.ro_db, {
            'peer_id': 'peer2',
            'network_id': 'network1'
        })

        assert result is not None
        assert result['name'] == 'Bob'
        assert result['user_id'] == 'user2'

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_user_by_peer_id_not_found(self):
        """Test getting user by non-existent peer ID."""
        result = get_user_by_peer_id(self.ro_db, {
            'peer_id': 'nonexistent',
            'network_id': 'network1'
        })

        assert result is None

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_user_by_peer_id_wrong_network(self):
        """Test getting user by peer ID in wrong network."""
        result = get_user_by_peer_id(self.ro_db, {
            'peer_id': 'peer1',
            'network_id': 'network2'
        })

        assert result is None

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_count_users(self):
        """Test counting users."""
        # Count users in network1
        result = count_users(self.ro_db, {'network_id': 'network1'})
        assert result == 2

        # Count users in network2
        result = count_users(self.ro_db, {'network_id': 'network2'})
        assert result == 1

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_is_user_in_network(self):
        """Test checking if user is in network."""
        # User in network
        result = is_user_in_network(self.ro_db, {
            'peer_id': 'peer1',
            'network_id': 'network1'
        })
        assert result is True

        # User not in network
        result = is_user_in_network(self.ro_db, {
            'peer_id': 'peer1',
            'network_id': 'network2'
        })
        assert result is False

        # Non-existent user
        result = is_user_in_network(self.ro_db, {
            'peer_id': 'nonexistent',
            'network_id': 'network1'
        })
        assert result is False

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_users_with_limit(self):
        """Test getting users with limit."""
        result = get_users(self.ro_db, {
            'identity_id': 'test_identity',
            'network_id': 'network1',
            'limit': 1
        })

        assert len(result) == 1

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_users_with_offset(self):
        """Test getting users with offset."""
        # Get all users first to know the order
        all_users = get_users(self.ro_db, {
            'identity_id': 'test_identity',
            'network_id': 'network1'
        })

        # Get users with offset
        result = get_users(self.ro_db, {
            'identity_id': 'test_identity',
            'network_id': 'network1',
            'offset': 1
        })

        assert len(result) == 1
        assert result[0]['user_id'] == all_users[1]['user_id']

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_users_ordered_by_joined_at(self):
        """Test that users are ordered by joined_at DESC."""
        result = get_users(self.ro_db, {
            'identity_id': 'test_identity',
            'network_id': 'network1'
        })

        # Should be in reverse order: Bob (2000), Alice (1000)
        assert result[0]['name'] == 'Bob'
        assert result[1]['name'] == 'Alice'
