"""
Tests for group event type query (list).
"""
import pytest
import sys
from pathlib import Path

# Add project root to path
test_dir = Path(__file__).parent
protocol_dir = test_dir.parent.parent.parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from protocols.quiet.events.group.queries import get as get_groups


class TestGroupQuery:
    """Test group list query."""

    @pytest.fixture
    def setup_groups(self, initialized_db):
        """Create test groups directly in database."""
        conn = initialized_db
        # Use a test identity ID directly
        identity_id = "test_identity"

        # Insert test networks
        network1_id = "test-network-1"
        network2_id = "test-network-2"

        conn.execute("""
            INSERT INTO networks (network_id, name, creator_id, created_at)
            VALUES (?, ?, ?, ?), (?, ?, ?, ?)
        """, (
            network1_id, "Network 1", "creator-1", 1000000,
            network2_id, "Network 2", "creator-2", 1000001
        ))

        # Insert test groups (both creator_id and owner_id are required)
        conn.execute("""
            INSERT INTO groups (group_id, name, network_id, creator_id, owner_id, created_at)
            VALUES
                ('group-eng', 'Engineering', ?, 'owner-1', 'owner-1', 1000002),
                ('group-mkt', 'Marketing', ?, 'owner-1', 'owner-1', 1000003),
                ('group-sales', 'Sales', ?, 'owner-2', 'owner-2', 1000004)
        """, (network1_id, network1_id, network2_id))

        # Insert group members
        conn.execute("""
            INSERT INTO group_members (group_id, user_id, added_by, added_at)
            VALUES
                ('group-eng', 'user-1', 'owner-1', 1000005),
                ('group-eng', 'user-2', 'owner-1', 1000006),
                ('group-mkt', 'user-1', 'owner-1', 1000007)
        """)

        conn.commit()

        return {
            "identity_id": identity_id,
            "network1_id": network1_id,
            "network2_id": network2_id,
            "group_ids": ["group-eng", "group-mkt", "group-sales"],
        }

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_all_groups(self, initialized_db, setup_groups):
        """Test listing all groups."""
        params = {"identity_id": setup_groups["identity_id"]}
        groups = get_groups(initialized_db, params)

        # Should return all 3 groups
        assert len(groups) == 3

        # Check group names
        group_names = [g["name"] for g in groups]
        assert "Engineering" in group_names
        assert "Marketing" in group_names
        assert "Sales" in group_names

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_groups_by_network(self, initialized_db, setup_groups):
        """Test listing groups filtered by network."""
        # Get groups from network 1
        params = {"identity_id": setup_groups["identity_id"], "network_id": setup_groups["network1_id"]}
        groups = get_groups(initialized_db, params)

        # Should return 2 groups (Engineering and Marketing)
        assert len(groups) == 2
        group_names = [g["name"] for g in groups]
        assert "Engineering" in group_names
        assert "Marketing" in group_names
        assert "Sales" not in group_names

        # Get groups from network 2
        params = {"identity_id": setup_groups["identity_id"], "network_id": setup_groups["network2_id"]}
        groups = get_groups(initialized_db, params)

        # Should return 1 group (Sales)
        assert len(groups) == 1
        assert groups[0]["name"] == "Sales"

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_groups_by_owner(self, initialized_db, setup_groups):
        """Test listing groups filtered by owner."""
        # Get groups owned by owner-1
        params = {"identity_id": setup_groups["identity_id"], "owner_id": "owner-1"}
        groups = get_groups(initialized_db, params)

        # Should return 2 groups
        assert len(groups) == 2
        group_names = [g["name"] for g in groups]
        assert "Engineering" in group_names
        assert "Marketing" in group_names

        # Get groups owned by owner-2
        params = {"identity_id": setup_groups["identity_id"], "owner_id": "owner-2"}
        groups = get_groups(initialized_db, params)

        # Should return 1 group
        assert len(groups) == 1
        assert groups[0]["name"] == "Sales"

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_groups_combined_filters(self, initialized_db, setup_groups):
        """Test listing groups with multiple filters."""
        # Get groups in network1 owned by owner-1
        params = {
            "identity_id": setup_groups["identity_id"],
            "network_id": setup_groups["network1_id"],
            "owner_id": "owner-1",
        }
        groups = get_groups(initialized_db, params)

        # Should return 2 groups
        assert len(groups) == 2

        # Get groups in network2 owned by owner-1 (should be none)
        params = {
            "identity_id": setup_groups["identity_id"],
            "network_id": setup_groups["network2_id"],
            "owner_id": "owner-1",
        }
        groups = get_groups(initialized_db, params)

        # Should return 0 groups
        assert len(groups) == 0

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_groups_includes_member_count(self, initialized_db, setup_groups):
        """Test that group listing includes member count."""
        params = {"identity_id": setup_groups["identity_id"], "network_id": setup_groups["network1_id"]}
        groups = get_groups(initialized_db, params)

        # Find Engineering group
        eng_group = next(g for g in groups if g["name"] == "Engineering")
        assert eng_group["member_count"] == 2

        # Find Marketing group
        mkt_group = next(g for g in groups if g["name"] == "Marketing")
        assert mkt_group["member_count"] == 1

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_groups_empty_result(self, initialized_db, setup_groups):
        """Test listing groups with no matches."""
        params = {"identity_id": setup_groups["identity_id"], "network_id": "non-existent-network"}
        groups = get_groups(initialized_db, params)

        # Should return empty list
        assert groups == []

    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_groups_ordering(self, initialized_db, setup_groups):
        """Test that groups are ordered by creation time."""
        params = {"identity_id": setup_groups["identity_id"]}
        groups = get_groups(initialized_db, params)

        # Should be ordered by created_at (newest first by default)
        assert groups[0]["name"] == "Sales"  # Created last (1000004)
        assert groups[1]["name"] == "Marketing"  # Created second (1000003)
        assert groups[2]["name"] == "Engineering"  # Created first (1000002)
