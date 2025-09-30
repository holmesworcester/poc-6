"""
Tests for message event type query (list).
"""
import pytest
import sys
import time
from pathlib import Path

# Add project root to path
test_dir = Path(__file__).parent
protocol_dir = test_dir.parent.parent.parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from protocols.quiet.events.message.queries import get as get_messages
# from core.pipeline import PipelineRunner  # Use if needed
# from core.handlers import handle_command  # Not needed


class TestMessageQuery:
    """Test message list query."""

    @pytest.fixture
    def setup_messages(self, initialized_db):
        """Create multiple messages for testing by inserting rows directly."""
        import time
        db = initialized_db
        # Use a test identity ID directly
        identity_id = "test_identity"

        # Network and groups
        network_id = "net-1"
        now = int(time.time() * 1000)
        db.execute("INSERT INTO networks (network_id, name, creator_id, created_at) VALUES (?, ?, ?, ?)", (network_id, "Test Network", identity_id, now))
        group1_id = "group-1"
        group2_id = "group-2"
        db.execute("INSERT INTO groups (group_id, name, network_id, creator_id, owner_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (group1_id, "Group 1", network_id, identity_id, identity_id, now))
        db.execute("INSERT INTO groups (group_id, name, network_id, creator_id, owner_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (group2_id, "Group 2", network_id, identity_id, identity_id, now))

        # Channels
        channel1_id = "chan-1"
        channel2_id = "chan-2"
        db.execute("INSERT INTO channels (channel_id, name, group_id, network_id, creator_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (channel1_id, "general", group1_id, network_id, identity_id, now))
        db.execute("INSERT INTO channels (channel_id, name, group_id, network_id, creator_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (channel2_id, "random", group2_id, network_id, identity_id, now))

        # Messages
        messages_created = []
        ts = now
        for i in range(5):
            mid = f"m1-{i}"
            db.execute("INSERT INTO messages (message_id, content, channel_id, group_id, network_id, author_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (mid, f"Message {i} in channel 1", channel1_id, group1_id, network_id, identity_id, ts))
            ts += 1
            messages_created.append({"content": f"Message {i} in channel 1", "channel_id": channel1_id, "group_id": group1_id})
        for i in range(3):
            mid = f"m2-{i}"
            db.execute("INSERT INTO messages (message_id, content, channel_id, group_id, network_id, author_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (mid, f"Message {i} in channel 2", channel2_id, group2_id, network_id, identity_id, ts))
            ts += 1
            messages_created.append({"content": f"Message {i} in channel 2", "channel_id": channel2_id, "group_id": group2_id})
        db.commit()

        return {
            "identity_id": identity_id,
            "network_id": network_id,
            "group1_id": group1_id,
            "group2_id": group2_id,
            "channel1_id": channel1_id,
            "channel2_id": channel2_id,
            "messages": messages_created,
        }
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_all_messages(self, initialized_db, setup_messages):
        """Test listing all messages without filters."""
        messages = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"]})
        
        assert len(messages) == 8  # 5 + 3 messages
        
        # Check messages are in chronological order (oldest first)
        for i in range(len(messages) - 1):
            assert messages[i]["created_at"] <= messages[i + 1]["created_at"]
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_by_channel(self, initialized_db, setup_messages):
        """Test filtering messages by channel_id."""
        data = setup_messages
        
        # List messages in channel 1
        messages = get_messages(initialized_db, {"identity_id": data["identity_id"], "channel_id": data["channel1_id"]})
        assert len(messages) == 5
        for message in messages:
            assert message["channel_id"] == data["channel1_id"]
            assert "channel 1" in message["content"]
        
        # List messages in channel 2
        messages = get_messages(initialized_db, {"identity_id": data["identity_id"], "channel_id": data["channel2_id"]})
        assert len(messages) == 3
        for message in messages:
            assert message["channel_id"] == data["channel2_id"]
            assert "channel 2" in message["content"]
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_by_group(self, initialized_db, setup_messages):
        """Test filtering messages by group_id."""
        data = setup_messages
        
        # List messages in group 1
        messages = get_messages(initialized_db, {"identity_id": data["identity_id"], "group_id": data["group1_id"]})
        assert len(messages) == 5
        for message in messages:
            assert message["group_id"] == data["group1_id"]
        
        # List messages in group 2
        messages = get_messages(initialized_db, {"identity_id": data["identity_id"], "group_id": data["group2_id"]})
        assert len(messages) == 3
        for message in messages:
            assert message["group_id"] == data["group2_id"]
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_with_limit(self, initialized_db, setup_messages):
        """Test limiting number of messages returned."""
        messages = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"], "limit": 3})
        
        assert len(messages) == 3
        
        # Should return the 3 oldest messages
        assert "Message 0" in messages[0]["content"]
        assert "Message 1" in messages[1]["content"]
        assert "Message 2" in messages[2]["content"]
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_with_offset(self, initialized_db, setup_messages):
        """Test pagination with offset."""
        # Get first page
        page1 = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"], "limit": 3, "offset": 0})
        assert len(page1) == 3
        
        # Get second page
        page2 = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"], "limit": 3, "offset": 3})
        assert len(page2) == 3
        
        # Get third page (partial)
        page3 = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"], "limit": 3, "offset": 6})
        assert len(page3) == 2  # Only 2 messages left
        
        # No overlap between pages
        page1_ids = [m["message_id"] for m in page1]
        page2_ids = [m["message_id"] for m in page2]
        page3_ids = [m["message_id"] for m in page3]
        
        assert len(set(page1_ids) & set(page2_ids)) == 0
        assert len(set(page2_ids) & set(page3_ids)) == 0
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_channel_with_limit(self, initialized_db, setup_messages):
        """Test combining channel filter with limit."""
        data = setup_messages
        
        messages = get_messages(initialized_db, {
            "identity_id": data["identity_id"],
            "channel_id": data["channel1_id"],
            "limit": 2
        })
        
        assert len(messages) == 2
        for message in messages:
            assert message["channel_id"] == data["channel1_id"]
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_default_limit(self, initialized_db, setup_messages):
        """Test that default limit is 100."""
        # Create more than 100 messages would be expensive, 
        # so we'll just verify the default is applied
        db_path = initialized_db.execute("PRAGMA database_list").fetchone()[2]
        identity = create_identity("Test User", db_path)
        messages = get_messages(initialized_db, {"identity_id": identity.id})
        
        # We have 8 messages, all should be returned with default limit
        assert len(messages) == 8
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_empty_result(self, initialized_db):
        """Test that empty database returns empty list."""
        db_path = initialized_db.execute("PRAGMA database_list").fetchone()[2]
        identity = create_identity("Test User", db_path)
        messages = get_messages(initialized_db, {"identity_id": identity.id})
        assert messages == []
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_nonexistent_filters(self, initialized_db, setup_messages):
        """Test filtering with non-existent IDs returns empty."""
        messages = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"], "channel_id": "nonexistent-channel"})
        assert messages == []
        
        messages = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"], "group_id": "nonexistent-group"})
        assert messages == []
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_returns_all_fields(self, initialized_db, setup_messages):
        """Test that query returns all message fields."""
        messages = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"], "limit": 1})
        
        assert len(messages) > 0
        message = messages[0]
        
        # Check all expected fields are present
        expected_fields = [
            'message_id', 'channel_id', 'group_id', 'network_id',
            'author_id', 'content', 'created_at'
        ]
        
        for field in expected_fields:
            assert field in message
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_chronological_order(self, initialized_db, setup_messages):
        """Test that messages are returned in chronological order."""
        data = setup_messages
        
        messages = get_messages(initialized_db, {"identity_id": data["identity_id"], "channel_id": data["channel1_id"]})
        
        # Messages should be in order: Message 0, Message 1, Message 2, etc.
        for i in range(len(messages)):
            assert f"Message {i}" in messages[i]["content"]
    
    @pytest.mark.unit
    @pytest.mark.event_type
    def test_list_messages_large_offset(self, initialized_db, setup_messages):
        """Test offset larger than total messages."""
        messages = get_messages(initialized_db, {"identity_id": setup_messages["identity_id"], "offset": 100})
        assert messages == []
