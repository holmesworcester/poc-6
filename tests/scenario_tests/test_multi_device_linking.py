"""
Scenario test: Multi-device linking.

Alice creates a network on her phone, then links her laptop to the same user account.
Both devices can send/receive messages and see the same conversation.

Tests:
- Alice creates network on phone
- Alice links laptop to her account via link URL
- Both devices have same user_id but different peer_ids
- Laptop receives group keys via GKS after linking
- Both devices can send and receive messages
- Network sees both devices as belonging to same user
"""
import sqlite3
from db import Database
import schema
from events.identity import user, link_invite, link
from events.content import message
from events.transit import sync


def test_alice_links_phone_to_laptop():
    """Alice links her phone and laptop via link URL."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Alice creates network on phone ===")

    # Alice creates a network on her phone
    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network on phone")
    print(f"  peer_id={alice_phone['peer_id'][:20]}...")
    print(f"  user_id={alice_phone['user_id'][:20]}...")
    print(f"  channel_id={alice_phone['channel_id'][:20]}...")

    db.commit()

    # Alice creates a link invite on her phone for her laptop
    print("\n=== Alice creates link invite for laptop ===")

    link_invite_id, link_url, link_data = link_invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db
    )
    print(f"Link invite created: {link_invite_id[:20]}...")
    print(f"Link URL: {link_url[:50]}...")

    db.commit()

    # Alice joins on her laptop via the link URL
    print("\n=== Alice links laptop to her account ===")

    alice_laptop = link.join(
        link_url=link_url,
        t_ms=3000,
        db=db
    )
    print(f"Alice linked laptop")
    print(f"  peer_id={alice_laptop['peer_id'][:20]}...")
    print(f"  user_id={alice_laptop['user_id'][:20]}...")

    db.commit()

    # Verify both devices have the same user_id
    assert alice_laptop['user_id'] == alice_phone['user_id'], \
        f"Both devices should have same user_id"
    print(f"✅ Both devices share user_id: {alice_laptop['user_id'][:20]}...")

    # Verify devices have different peer_ids
    assert alice_laptop['peer_id'] != alice_phone['peer_id'], \
        f"Devices should have different peer_ids"
    print(f"✅ Devices have different peer_ids")

    # Initial sync to converge (multiple rounds for GKS propagation)
    print("\n=== Initial sync to propagate link event and group keys ===")

    for i in range(20):
        print(f"Sync round {i+1}...")
        sync.send_request_to_all(t_ms=4000 + i*100, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=4050 + i*100, db=db)
        db.commit()

    # Verify laptop has the group key (from GKS)
    laptop_has_key = db.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (alice_phone['key_id'], alice_laptop['peer_id'])
    )
    print(f"Laptop has group key: {bool(laptop_has_key)}")

    assert laptop_has_key, \
        f"Laptop should have Alice's group key after sync (received via group_key_shared)"
    print(f"✅ Laptop received group key via GKS")

    # Verify laptop has valid channel
    laptop_channel_valid = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_phone['channel_id'], alice_laptop['peer_id'])
    )
    print(f"Laptop has valid channel: {bool(laptop_channel_valid)}")

    assert laptop_channel_valid, \
        f"Laptop should have valid channel after bootstrap"
    print(f"✅ Laptop has valid channel")

    # Create messages on both devices
    print("\n=== Creating messages on both devices ===")

    # Alice sends from phone
    alice_phone_msg = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Hello from Alice's phone!",
        t_ms=5000,
        db=db
    )
    db.commit()
    print(f"Alice (phone) created message: {alice_phone_msg['id'][:20]}...")

    # Alice sends from laptop
    alice_laptop_msg = message.create(
        peer_id=alice_laptop['peer_id'],
        channel_id=alice_laptop['channel_id'],
        content="Hello from Alice's laptop!",
        t_ms=5100,
        db=db
    )
    db.commit()
    print(f"Alice (laptop) created message: {alice_laptop_msg['id'][:20]}...")

    # Sync messages between devices
    print("\n=== Sync messages between devices ===")

    for round_num in range(3):
        sync.send_request_to_all(t_ms=6000 + round_num * 100, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=6050 + round_num * 100, db=db)
        db.commit()

    # Verify both devices see both messages
    print("\n=== Verifying message delivery ===")

    phone_messages = message.list_messages(alice_phone['channel_id'], alice_phone['peer_id'], db)
    laptop_messages = message.list_messages(alice_laptop['channel_id'], alice_laptop['peer_id'], db)

    phone_contents = [msg['content'] for msg in phone_messages]
    laptop_contents = [msg['content'] for msg in laptop_messages]

    print(f"Phone sees {len(phone_messages)} messages: {phone_contents}")
    print(f"Laptop sees {len(laptop_messages)} messages: {laptop_contents}")

    # Phone should see both messages
    assert "Hello from Alice's phone!" in phone_contents, \
        "Phone should see its own message"
    assert "Hello from Alice's laptop!" in phone_contents, \
        "Phone should see laptop's message"

    # Laptop should see both messages
    assert "Hello from Alice's phone!" in laptop_contents, \
        "Laptop should see phone's message"
    assert "Hello from Alice's laptop!" in laptop_contents, \
        "Laptop should see its own message"

    print(f"\n✅ All assertions passed!")


def test_alice_laptop_joins_after_phone_has_messages():
    """Alice's laptop joins after phone already has messages."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Alice creates network and sends messages ===")

    # Alice creates network on phone
    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Alice sends messages from phone first
    msg1 = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Message 1 from phone",
        t_ms=1500,
        db=db
    )
    db.commit()

    msg2 = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="Message 2 from phone",
        t_ms=1600,
        db=db
    )
    db.commit()

    print(f"Phone sent 2 messages")

    # Now link laptop
    print("\n=== Link laptop after messages already exist ===")

    link_invite_id, link_url, link_data = link_invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db
    )
    db.commit()

    alice_laptop = link.join(
        link_url=link_url,
        t_ms=3000,
        db=db
    )
    db.commit()

    # Sync to propagate messages to laptop
    print("\n=== Sync messages to laptop ===")

    for i in range(5):
        sync.send_request_to_all(t_ms=4000 + i*200, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=4100 + i*200, db=db)
        db.commit()

    # Laptop should see all historical messages
    laptop_messages = message.list_messages(
        alice_laptop['channel_id'],
        alice_laptop['peer_id'],
        db
    )
    laptop_contents = [msg['content'] for msg in laptop_messages]

    print(f"Laptop sees {len(laptop_messages)} messages: {laptop_contents}")

    assert len(laptop_messages) == 2, \
        f"Laptop should see both historical messages, got {len(laptop_messages)}"
    assert "Message 1 from phone" in laptop_contents
    assert "Message 2 from phone" in laptop_contents

    print(f"✅ Laptop received all historical messages after linking!")


def test_three_devices_all_linked():
    """Alice links phone, laptop, and tablet - all three share messages."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Create network on phone ===")

    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Link laptop
    print("\n=== Link laptop ===")

    link_invite_id_1, link_url_1, _ = link_invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db
    )
    db.commit()

    alice_laptop = link.join(link_url=link_url_1, t_ms=3000, db=db)
    db.commit()

    # Link tablet
    print("\n=== Link tablet ===")

    link_invite_id_2, link_url_2, _ = link_invite.create(
        peer_id=alice_phone['peer_id'],  # Can create from phone or laptop
        t_ms=4000,
        db=db
    )
    db.commit()

    alice_tablet = link.join(link_url=link_url_2, t_ms=5000, db=db)
    db.commit()

    # All three should have same user_id
    assert alice_laptop['user_id'] == alice_phone['user_id']
    assert alice_tablet['user_id'] == alice_phone['user_id']
    print(f"✅ All three devices share user_id: {alice_phone['user_id'][:20]}...")

    # Sync
    print("\n=== Sync all devices ===")

    for i in range(6):
        sync.send_request_to_all(t_ms=6000 + i*200, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=6100 + i*200, db=db)
        db.commit()

    # Each device sends a message
    print("\n=== Each device sends a message ===")

    msg_phone = message.create(
        peer_id=alice_phone['peer_id'],
        channel_id=alice_phone['channel_id'],
        content="From phone",
        t_ms=7000,
        db=db
    )
    db.commit()

    msg_laptop = message.create(
        peer_id=alice_laptop['peer_id'],
        channel_id=alice_laptop['channel_id'],
        content="From laptop",
        t_ms=7100,
        db=db
    )
    db.commit()

    msg_tablet = message.create(
        peer_id=alice_tablet['peer_id'],
        channel_id=alice_tablet['channel_id'],
        content="From tablet",
        t_ms=7200,
        db=db
    )
    db.commit()

    # Sync messages
    print("\n=== Sync messages ===")

    for i in range(4):
        sync.send_request_to_all(t_ms=8000 + i*100, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=8050 + i*100, db=db)
        db.commit()

    # All devices should see all three messages
    phone_msgs = message.list_messages(alice_phone['channel_id'], alice_phone['peer_id'], db)
    laptop_msgs = message.list_messages(alice_laptop['channel_id'], alice_laptop['peer_id'], db)
    tablet_msgs = message.list_messages(alice_tablet['channel_id'], alice_tablet['peer_id'], db)

    print(f"Phone sees: {[m['content'] for m in phone_msgs]}")
    print(f"Laptop sees: {[m['content'] for m in laptop_msgs]}")
    print(f"Tablet sees: {[m['content'] for m in tablet_msgs]}")

    assert len(phone_msgs) == 3
    assert len(laptop_msgs) == 3
    assert len(tablet_msgs) == 3

    contents_phone = [m['content'] for m in phone_msgs]
    for device_msg in ["From phone", "From laptop", "From tablet"]:
        assert device_msg in contents_phone

    print(f"✅ All three devices see all messages!")
