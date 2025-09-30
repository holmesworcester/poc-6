"""
Tests for channel event type query (list).
"""
import pytest
import sys
from pathlib import Path

# Add project root to path
test_dir = Path(__file__).parent
protocol_dir = test_dir.parent.parent.parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from protocols.quiet.events.channel.queries import get as get_channels
# from core.pipeline import PipelineRunner  # Use if needed


class TestChannelQuery:
    """Test channel list query."""

    @pytest.fixture
    def setup_channels(self, initialized_db):
        """Create multiple channels for testing by inserting rows directly."""
        import time
        db = initialized_db
        # Use a test identity ID directly
        identity_id = "test_identity"

        # Create two networks
        network1_id = "net-1"
        network2_id = "net-2"
        now = int(time.time() * 1000)
        db.execute("INSERT INTO networks (network_id, name, creator_id, created_at) VALUES (?, ?, ?, ?)", (network1_id, "Network 1", identity_id, now))
        db.execute("INSERT INTO networks (network_id, name, creator_id, created_at) VALUES (?, ?, ?, ?)", (network2_id, "Network 2", identity_id, now))

        # Create groups
        group1_id = "group-1"
        group2_id = "group-2"
        db.execute("INSERT INTO groups (group_id, name, network_id, creator_id, owner_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (group1_id, "Group 1", network1_id, identity_id, identity_id, now))
        db.execute("INSERT INTO groups (group_id, name, network_id, creator_id, owner_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (group2_id, "Group 2", network2_id, identity_id, identity_id, now))

        # Create channels
        channels_created = []
        c1 = ("chan-1", "general", group1_id, network1_id, identity_id, now, "general channel")
        c2 = ("chan-2", "random", group1_id, network1_id, identity_id, now, "random channel")
        c3 = ("chan-3", "announcements", group2_id, network2_id, identity_id, now, "announcements channel")
        db.execute("INSERT INTO channels (channel_id, name, group_id, network_id, creator_id, created_at, description) VALUES (?, ?, ?, ?, ?, ?, ?)", c1)
        db.execute("INSERT INTO channels (channel_id, name, group_id, network_id, creator_id, created_at, description) VALUES (?, ?, ?, ?, ?, ?, ?)", c2)
        db.execute("INSERT INTO channels (channel_id, name, group_id, network_id, creator_id, created_at, description) VALUES (?, ?, ?, ?, ?, ?, ?)", c3)
        db.commit()

        channels_created.extend([
            {"channel_id": c1[0], "group_id": group1_id, "name": c1[1], "network_id": network1_id},
            {"channel_id": c2[0], "group_id": group1_id, "name": c2[1], "network_id": network1_id},
            {"channel_id": c3[0], "group_id": group2_id, "name": c3[1], "network_id": network2_id},
        ])

        return {
            "identity_id": identity_id,
            "network1_id": network1_id,
            "network2_id": network2_id,
            "group1_id": group1_id,
            "group2_id": group2_id,
            "channels": channels_created
        }
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_all_channels(self, initialized_db, setup_channels):
        """Test listing all channels without filters."""
        channels = get_channels(initialized_db, {"identity_id": setup_channels["identity_id"]})
        
        assert len(channels) == 3
        
        # Check channels are sorted by created_at DESC
        for i in range(len(channels) - 1):
            assert channels[i]["created_at"] >= channels[i + 1]["created_at"]
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_channels_by_group(self, initialized_db, setup_channels):
        """Test filtering channels by group_id."""
        data = setup_channels
        
        # List channels in group 1
        channels = get_channels(initialized_db, {"identity_id": data["identity_id"], "group_id": data["group1_id"]})
        assert len(channels) == 2
        for channel in channels:
            assert channel["group_id"] == data["group1_id"]
            assert channel["name"] in ["general", "random"]
        
        # List channels in group 2
        channels = get_channels(initialized_db, {"identity_id": data["identity_id"], "group_id": data["group2_id"]})
        assert len(channels) == 1
        assert channels[0]["group_id"] == data["group2_id"]
        assert channels[0]["name"] == "announcements"
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_channels_by_network(self, initialized_db, setup_channels):
        """Test filtering channels by network_id."""
        data = setup_channels
        
        # List channels in network 1
        channels = get_channels(initialized_db, {"identity_id": data["identity_id"], "network_id": data["network1_id"]})
        assert len(channels) == 2
        for channel in channels:
            assert channel["network_id"] == data["network1_id"]
        
        # List channels in network 2
        channels = get_channels(initialized_db, {"identity_id": data["identity_id"], "network_id": data["network2_id"]})
        assert len(channels) == 1
        assert channels[0]["network_id"] == data["network2_id"]
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_channels_by_group_and_network(self, initialized_db, setup_channels):
        """Test filtering channels by both group_id and network_id."""
        data = setup_channels
        
        # Should return channels matching both filters
        channels = get_channels(initialized_db, {
            "identity_id": data["identity_id"],
            "group_id": data["group1_id"],
            "network_id": data["network1_id"]
        })
        
        assert len(channels) == 2
        for channel in channels:
            assert channel["group_id"] == data["group1_id"]
            assert channel["network_id"] == data["network1_id"]
        
        # Mismatched filters should return empty
        channels = get_channels(initialized_db, {
            "identity_id": data["identity_id"],
            "group_id": data["group1_id"],
            "network_id": data["network2_id"]  # Group 1 is in network 1, not 2
        })
        assert len(channels) == 0
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_channels_empty_result(self, initialized_db):
        """Test that empty database returns empty list."""
        db_path = initialized_db.execute("PRAGMA database_list").fetchone()[2]
        identity = create_identity("Test User", db_path)
        channels = get_channels(initialized_db, {"identity_id": identity.id})
        assert channels == []
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_channels_nonexistent_filters(self, initialized_db, setup_channels):
        """Test filtering with non-existent IDs returns empty."""
        channels = get_channels(initialized_db, {"identity_id": setup_channels["identity_id"], "group_id": "nonexistent-group"})
        assert channels == []
        
        channels = get_channels(initialized_db, {"identity_id": setup_channels["identity_id"], "network_id": "nonexistent-network"})
        assert channels == []
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_get_channels_returns_all_fields(self, initialized_db, setup_channels):
        """Test that query returns all channel fields."""
        channels = get_channels(initialized_db, {"identity_id": setup_channels["identity_id"]})
        
        assert len(channels) > 0
        channel = channels[0]
        
        # Check all expected fields are present
        expected_fields = [
            'channel_id', 'group_id', 'network_id', 
            'name', 'creator_id', 'created_at', 'description'
        ]
        
        for field in expected_fields:
            assert field in channel
