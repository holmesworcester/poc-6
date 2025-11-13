"""
Scenario test: Bidirectional messaging between linked devices.

Alice links two devices and verifies that messages sync bidirectionally.
Both devices should be able to send and receive messages from each other.

Tests:
- Alice creates network and links second device
- Device 1 sends message M1
- Device 2 sends message M2
- Both devices see both messages
- Messages sync correctly in both directions
"""
import sqlite3
from db import Database
import schema
from events.identity import user, link_invite, link
from events.group import group
from events.content import message
import tick


def test_linked_devices_bidirectional_messaging():
    """Messages sync bidirectionally between linked devices."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Alice creates network ===")

    # Alice creates network
    alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network on device 1")
    print(f"  user_id={alice_device1['user_id'][:20]}...")
    db.commit()

    # Create a test group for messaging and add Alice as member
    print("\n=== Create test group ===")

    group_id, group_key_id = group.create(
        name='Test Group',
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=1500,
        db=db
    )
    print(f"Created Test Group: {group_id[:20]}...")
    group_member.create(
        group_id=group_id,
        user_id=alice_device1['user_id'],
        peer_id=alice_device1['peer_id'],
        peer_shared_id=alice_device1['peer_shared_id'],
        t_ms=1501,
        db=db
    )
    db.commit()

    # Sync for group creation
    for i in range(5):
        tick.tick(t_ms=2000 + i*100, db=db)

    # Alice creates link invite and links second device
    print("\n=== Link second device ===")

    invite_id, invite_link, _ = link_invite.create(
        peer_id=alice_device1['peer_id'],
        t_ms=3000,
        db=db
    )
    db.commit()

    alice_device2 = link.join(
        link_url=invite_link,
        t_ms=4000,
        db=db
    )
    print(f"Alice linked device 2")
    print(f"  user_id={alice_device2['user_id'][:20]}...")
    db.commit()

    # Verify same user_id
    assert alice_device2['user_id'] == alice_device1['user_id']
    print(f"✅ Both devices share user_id")

    # Sync for link convergence and group key sharing
    print("\n=== Sync for link convergence ===")
    for i in range(20):
        print(f"Sync round {i+1}...")
        tick.tick(t_ms=5000 + i*200, db=db)

    # Verify device 2 has the group key
    has_key = db.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (group_key_id, alice_device2['peer_id'])
    )
    assert has_key, "Device 2 should have group key"
    print("✅ Device 2 has group key")

    # Device 1 sends message M1
    print("\n=== Device 1 sends message M1 ===")

    msg1_id = message.create(
        peer_id=alice_device1['peer_id'],
        channel_id=alice_device1['channel_id'],
        content="Message from device 1",
        t_ms=6000,
        db=db
    )
    print(f"Device 1 sent message: {msg1_id[:20]}...")
    db.commit()

    # Device 2 sends message M2
    print("\n=== Device 2 sends message M2 ===")

    msg2_id = message.create(
        peer_id=alice_device2['peer_id'],
        channel_id=alice_device2['channel_id'],
        content="Message from device 2",
        t_ms=6500,
        db=db
    )
    print(f"Device 2 sent message: {msg2_id[:20]}...")
    db.commit()

    # Sync for message convergence
    print("\n=== Sync for message convergence ===")
    for i in range(15):
        print(f"Sync round {i+1}...")
        tick.tick(t_ms=7000 + i*200, db=db)

    # Verify device 1 sees both messages
    print("\n=== Verifying device 1 sees both messages ===")

    device1_messages = message.list_messages(
        alice_device1['channel_id'],
        alice_device1['peer_id'],
        db
    )
    device1_contents = [msg['content'] for msg in device1_messages]

    print(f"Device 1 sees {len(device1_messages)} messages: {device1_contents}")

    assert "Message from device 1" in device1_contents, \
        "Device 1 should see its own message"
    assert "Message from device 2" in device1_contents, \
        "Device 1 should see device 2's message"

    print("✅ Device 1 sees both messages")

    # Verify device 2 sees both messages
    print("\n=== Verifying device 2 sees both messages ===")

    device2_messages = message.list_messages(
        alice_device2['channel_id'],
        alice_device2['peer_id'],
        db
    )
    device2_contents = [msg['content'] for msg in device2_messages]

    print(f"Device 2 sees {len(device2_messages)} messages: {device2_contents}")

    assert "Message from device 1" in device2_contents, \
        "Device 2 should see device 1's message"
    assert "Message from device 2" in device2_contents, \
        "Device 2 should see its own message"

    print("✅ Device 2 sees both messages")

    # Verify message count matches
    assert len(device1_messages) == len(device2_messages) == 2, \
        "Both devices should see exactly 2 messages"

    print(f"\n✅ All assertions passed! Bidirectional messaging works correctly.")
