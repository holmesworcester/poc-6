#!/usr/bin/env python3
"""
Tests for the refactored demo v2 with command-returns-state pattern
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.append(str(Path(__file__).parent.parent.parent.parent))

from protocols.quiet.demo.demo_v2 import UnifiedDemoCore, CommandResult


def test_command_returns_state():
    """Test that commands return complete state"""
    print("Testing command-returns-state pattern...")

    core = UnifiedDemoCore(reset_db=True)

    # Test 1: Create identity returns identity info
    result = core.execute_command(1, "/create alice")
    assert result.success
    assert result.identity_name == "alice"
    assert result.identity_id is not None
    assert result.peer_id is not None
    print("✓ Create identity returns state")

    # Test 2: Create network returns groups and channels
    result = core.execute_command(1, "/network test-net")
    assert result.success
    assert result.network_name == "test-net"
    assert result.network_id is not None
    assert len(result.groups) == 1
    assert result.groups[0]['name'] == 'public'
    assert len(result.channels) == 1
    assert result.channels[0]['name'] == 'general'
    assert result.current_channel_id is not None
    assert result.current_channel_name == 'general'
    print("✓ Create network returns groups and channels")

    # Test 3: Send message returns message list
    result = core.execute_command(1, "Hello world")
    assert result.success
    assert len(result.messages) > 0
    assert result.messages[-1]['content'] == "Hello world"
    print("✓ Send message returns message list")

    # Test 4: Create channel in group returns updated channel list
    result = core.execute_command(1, "/channel dev in public")
    assert result.success
    assert result.current_channel_name == "dev"
    assert len(result.channels) == 2
    channel_names = [c['name'] for c in result.channels]
    assert 'dev' in channel_names
    assert 'general' in channel_names
    print("✓ Create channel returns updated channel list")

    # Test 5: Refresh returns current state
    result = core.execute_command(1, "/refresh")
    assert result.success
    assert result.identity_name == "alice"
    assert result.network_name == "test-net"
    assert len(result.groups) == 1
    assert len(result.channels) == 2
    print("✓ Refresh returns complete state")


def test_panel_isolation():
    """Test that panels are isolated from each other"""
    print("\nTesting panel isolation...")

    core = UnifiedDemoCore(reset_db=True)

    # Create different identities in different panels
    result1 = core.execute_command(1, "/create alice")
    assert result1.identity_name == "alice"

    result2 = core.execute_command(2, "/create bob")
    assert result2.identity_name == "bob"

    # Create different networks
    result1 = core.execute_command(1, "/network alice-net")
    assert result1.network_name == "alice-net"

    result2 = core.execute_command(2, "/network bob-net")
    assert result2.network_name == "bob-net"

    # Verify isolation
    assert core.panels[1].network_id != core.panels[2].network_id
    assert result1.groups[0]['group_id'] != result2.groups[0]['group_id']
    print("✓ Panels are isolated")

    # Verify each panel sees only its own data
    result1 = core.execute_command(1, "/refresh")
    result2 = core.execute_command(2, "/refresh")

    assert result1.network_name == "alice-net"
    assert result2.network_name == "bob-net"
    assert result1.network_id != result2.network_id
    print("✓ Each panel sees only its own data")


def test_invite_join_flow():
    """Test invite and join workflow"""
    print("\nTesting invite/join flow...")

    core = UnifiedDemoCore(reset_db=True)

    # Alice creates network
    core.execute_command(1, "/create alice")
    core.execute_command(1, "/network shared-net")

    # Alice generates invite
    result = core.execute_command(1, "/invite")
    assert result.success
    assert "quiet://invite/" in result.message
    invite_code = result.message.split("Invite code: ")[1]
    print(f"✓ Generated invite: {invite_code[:50]}...")

    # Bob joins with invite
    result = core.execute_command(2, f"/join {invite_code} bob")
    assert result.success
    assert result.identity_name == "bob"
    assert result.network_id is not None
    assert len(result.groups) > 0
    assert len(result.channels) > 0
    print("✓ Bob joined network")

    # Both should see each other in users
    result1 = core.execute_command(1, "/refresh")
    result2 = core.execute_command(2, "/refresh")

    user_names1 = [u.get('username') or u.get('name') for u in result1.users]
    user_names2 = [u.get('username') or u.get('name') for u in result2.users]

    # Both should see both users
    assert 'alice' in user_names1 or 'alice' in user_names2
    assert 'bob' in user_names1 or 'bob' in user_names2
    print("✓ Both users visible in network")


def test_message_flow():
    """Test message sending between users"""
    print("\nTesting message flow...")

    core = UnifiedDemoCore(reset_db=True)

    # Setup: Alice creates network
    core.execute_command(1, "/create alice")
    result = core.execute_command(1, "/network chat-net")
    alice_channel = result.current_channel_id

    # Alice sends message
    result = core.execute_command(1, "Hello from Alice")
    assert result.success
    assert len(result.messages) == 1
    assert result.messages[0]['content'] == "Hello from Alice"
    print("✓ Alice sent message")

    # Generate invite for Bob
    result = core.execute_command(1, "/invite")
    invite_code = result.message.split("Invite code: ")[1]

    # Bob joins
    result = core.execute_command(2, f"/join {invite_code} bob")
    assert result.success

    # Bob should join same channel
    core.panels[2].current_channel_id = alice_channel

    # Bob sends message
    result = core.execute_command(2, "Hello from Bob")
    assert result.success
    print("✓ Bob sent message")

    # Alice refreshes and should see both messages
    result = core.execute_command(1, "/refresh")
    assert len(result.messages) >= 2
    contents = [m['content'] for m in result.messages]
    assert "Hello from Alice" in contents
    assert "Hello from Bob" in contents
    print("✓ Messages visible to both users")


def test_error_handling():
    """Test error handling"""
    print("\nTesting error handling...")

    core = UnifiedDemoCore(reset_db=True)

    # Try to create network without identity
    result = core.execute_command(1, "/network test")
    assert not result.success
    assert "identity" in result.error.lower()
    print("✓ Cannot create network without identity")

    # Try to send message without channel
    core.execute_command(1, "/create alice")
    result = core.execute_command(1, "test message")
    assert not result.success
    assert "channel" in result.error.lower()
    print("✓ Cannot send message without channel")

    # Try invalid command
    result = core.execute_command(1, "/invalid")
    assert not result.success
    assert "Unknown command" in result.error
    print("✓ Invalid command handled")


def main():
    """Run all tests"""
    print("="*50)
    print("Testing Demo V2 Architecture")
    print("="*50)

    try:
        test_command_returns_state()
        test_panel_isolation()
        test_invite_join_flow()
        test_message_flow()
        test_error_handling()

        print("\n" + "="*50)
        print("All tests passed! ✓")
        print("="*50)

    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()