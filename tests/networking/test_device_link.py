"""Multi-device linking over real UDP.

Tests that a user can link multiple devices and sync messages
between them over real UDP sockets.
"""

import pytest
from events.identity import user, invite, peer, peer_shared
from events.content import message
from tests.networking.conftest import (
    RealClient, setup_transport_callback, tick_until, now_ms, get_unique_ports
)
import tempfile
import shutil
from core.db import Database
from core import schema, queues
import sqlite3


@pytest.fixture
def device_link_clients(tmp_dir):
    """Create clients for device link testing.

    Alice has two devices (alice_phone, alice_laptop).
    Bob has one device.
    """
    ports = get_unique_ports(3)

    alice_phone = RealClient("AlicePhone", f"{tmp_dir}/alice_phone.db", port=ports[0])
    alice_laptop = RealClient("AliceLaptop", f"{tmp_dir}/alice_laptop.db", port=ports[1])
    bob = RealClient("Bob", f"{tmp_dir}/bob.db", port=ports[2])

    clients = {"alice_phone": alice_phone, "alice_laptop": alice_laptop, "bob": bob}
    setup_transport_callback(clients)

    yield clients

    # Cleanup
    queues.set_transport_callback(None)
    alice_phone.stop()
    alice_laptop.stop()
    bob.stop()


def test_device_link_sync_messages(device_link_clients):
    """Test that linked devices can sync messages."""
    alice_phone = device_link_clients["alice_phone"]
    alice_laptop = device_link_clients["alice_laptop"]
    bob = device_link_clients["bob"]

    # Alice creates network on phone
    t_ms = now_ms()
    result = user.new_network(name="DeviceLinkNet", t_ms=t_ms, db=alice_phone.db)
    alice_phone.peer_id = result['peer_id']
    alice_phone.peer_shared_id = result['peer_shared_id']
    alice_phone.user_id = result['user_id']
    alice_phone.network_id = result['network_id']
    alice_phone.channel_id = result['channel_id']
    alice_phone.db.commit()

    # Create invite for Bob (mode='user')
    t_ms = now_ms()
    invite_id, invite_code, _ = invite.create(
        peer_id=alice_phone.peer_id,
        t_ms=t_ms,
        db=alice_phone.db,
        mode='user'  # Network join invite
    )
    alice_phone.db.commit()

    # Bob joins
    t_ms = now_ms()
    bob.peer_id = peer.create(t_ms=t_ms, db=bob.db)
    bob.db.commit()

    join_result = user.join(
        peer_id=bob.peer_id,
        invite_link=invite_code,
        name="Bob",
        device_name="BobDevice",
        t_ms=t_ms,
        db=bob.db
    )
    bob.peer_shared_id = join_result['peer_shared_id']
    bob.user_id = join_result['user_id']
    bob.network_id = join_result['network_id']
    bob.channel_id = join_result['channel_id']
    bob.db.commit()

    # Initial sync between phone and Bob
    two_clients = {"alice_phone": alice_phone, "bob": bob}
    tick_until(two_clients, lambda: False, max_ticks=30, tick_interval=0.05)

    # Create device link code on Alice's phone (mode='peer')
    t_ms = now_ms()
    link_invite_id, link_code, _ = invite.create(
        peer_id=alice_phone.peer_id,
        t_ms=t_ms,
        db=alice_phone.db,
        mode='peer',  # Device link invite
        user_id=alice_phone.user_id  # Link to Alice's user
    )
    alice_phone.db.commit()

    # Alice's laptop joins via device link
    t_ms = now_ms()
    alice_laptop.peer_id = peer.create(t_ms=t_ms, db=alice_laptop.db)
    alice_laptop.db.commit()

    # Accept the peer invite
    t_ms = now_ms()
    accepted = invite.accept(alice_laptop.peer_id, link_code, t_ms=t_ms, db=alice_laptop.db)
    assert accepted['mode'] == 'peer', f"Expected mode='peer', got {accepted['mode']}"
    alice_laptop.db.commit()

    # Join via peer_shared.join for device linking
    t_ms = now_ms()
    link_result = peer_shared.join(
        peer_id=alice_laptop.peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=t_ms,
        db=alice_laptop.db
    )
    alice_laptop.peer_shared_id = link_result['peer_shared_id']
    alice_laptop.user_id = link_result['user_id']
    alice_laptop.channel_id = alice_phone.channel_id  # Share the same channel
    alice_laptop.db.commit()

    # Verify same user_id
    assert alice_laptop.user_id == alice_phone.user_id, "Linked devices should share user_id"

    # Now all three clients sync - allow more time for device link sync
    tick_until(device_link_clients, lambda: False, max_ticks=100, tick_interval=0.05)

    # Wait for laptop to sync the channel before testing messages
    from events.content import channel
    def laptop_has_channel():
        channels = channel.list(alice_laptop.peer_id, alice_laptop.db)
        return any(c['channel_id'] == alice_phone.channel_id for c in channels)

    ticks = tick_until(device_link_clients, laptop_has_channel, max_ticks=100)
    assert ticks is not None, "Laptop didn't sync the channel"

    # Bob sends a message
    t_ms = now_ms()
    bob_msg = message.create(
        content="Hello from Bob!",
        channel_id=alice_phone.channel_id,
        peer_id=bob.peer_id,
        t_ms=t_ms,
        db=bob.db
    )
    bob.db.commit()
    bob_msg_id = bob_msg['id']

    # Wait for both of Alice's devices to receive it
    def both_alice_devices_have_bob_msg():
        phone_msgs = message.list(alice_phone.channel_id, alice_phone.peer_id, alice_phone.db)
        laptop_msgs = message.list(alice_phone.channel_id, alice_laptop.peer_id, alice_laptop.db)

        phone_has = any(m['message_id'] == bob_msg_id for m in phone_msgs)
        laptop_has = any(m['message_id'] == bob_msg_id for m in laptop_msgs)

        return phone_has and laptop_has

    ticks = tick_until(device_link_clients, both_alice_devices_have_bob_msg, max_ticks=150)
    assert ticks is not None, "Both Alice devices didn't receive Bob's message"

    # Alice's laptop sends a message
    t_ms = now_ms()
    laptop_msg = message.create(
        content="Hello from Alice's laptop!",
        channel_id=alice_phone.channel_id,
        peer_id=alice_laptop.peer_id,
        t_ms=t_ms,
        db=alice_laptop.db
    )
    alice_laptop.db.commit()
    laptop_msg_id = laptop_msg['id']

    # Wait for Bob and Alice's phone to receive it
    def bob_and_phone_have_laptop_msg():
        phone_msgs = message.list(alice_phone.channel_id, alice_phone.peer_id, alice_phone.db)
        bob_msgs = message.list(alice_phone.channel_id, bob.peer_id, bob.db)

        phone_has = any(m['message_id'] == laptop_msg_id for m in phone_msgs)
        bob_has = any(m['message_id'] == laptop_msg_id for m in bob_msgs)

        return phone_has and bob_has

    ticks = tick_until(device_link_clients, bob_and_phone_have_laptop_msg, max_ticks=150)
    assert ticks is not None, "Bob and Alice's phone didn't receive laptop message"

    # Final verification: count messages on each device
    phone_msgs = message.list(alice_phone.channel_id, alice_phone.peer_id, alice_phone.db)
    laptop_msgs = message.list(alice_phone.channel_id, alice_laptop.peer_id, alice_laptop.db)
    bob_msgs = message.list(alice_phone.channel_id, bob.peer_id, bob.db)

    # All should have at least the two messages we sent
    phone_contents = {m['content'] for m in phone_msgs}
    laptop_contents = {m['content'] for m in laptop_msgs}
    bob_contents = {m['content'] for m in bob_msgs}

    expected = {"Hello from Bob!", "Hello from Alice's laptop!"}

    assert expected.issubset(phone_contents), f"Phone missing: {expected - phone_contents}"
    assert expected.issubset(laptop_contents), f"Laptop missing: {expected - laptop_contents}"
    assert expected.issubset(bob_contents), f"Bob missing: {expected - bob_contents}"


def test_device_link_shares_history(device_link_clients):
    """Test that a newly linked device receives message history."""
    alice_phone = device_link_clients["alice_phone"]
    alice_laptop = device_link_clients["alice_laptop"]
    bob = device_link_clients["bob"]

    # Alice creates network on phone
    t_ms = now_ms()
    result = user.new_network(name="HistoryNet", t_ms=t_ms, db=alice_phone.db)
    alice_phone.peer_id = result['peer_id']
    alice_phone.peer_shared_id = result['peer_shared_id']
    alice_phone.user_id = result['user_id']
    alice_phone.network_id = result['network_id']
    alice_phone.channel_id = result['channel_id']
    alice_phone.db.commit()

    # Create invite for Bob
    t_ms = now_ms()
    invite_id, invite_code, _ = invite.create(
        peer_id=alice_phone.peer_id,
        t_ms=t_ms,
        db=alice_phone.db,
        mode='user'
    )
    alice_phone.db.commit()

    # Bob joins
    t_ms = now_ms()
    bob.peer_id = peer.create(t_ms=t_ms, db=bob.db)
    bob.db.commit()

    join_result = user.join(
        peer_id=bob.peer_id,
        invite_link=invite_code,
        name="Bob",
        device_name="BobDevice",
        t_ms=t_ms,
        db=bob.db
    )
    bob.peer_shared_id = join_result['peer_shared_id']
    bob.user_id = join_result['user_id']
    bob.db.commit()

    # Sync
    two_clients = {"alice_phone": alice_phone, "bob": bob}
    tick_until(two_clients, lambda: False, max_ticks=30, tick_interval=0.05)

    # Send several messages BEFORE laptop links
    historical_msgs = []
    for i in range(3):
        t_ms = now_ms()
        msg = message.create(
            content=f"Historical message {i+1}",
            channel_id=alice_phone.channel_id,
            peer_id=alice_phone.peer_id,
            t_ms=t_ms,
            db=alice_phone.db
        )
        alice_phone.db.commit()
        historical_msgs.append(msg['id'])

    # Sync these messages
    tick_until(two_clients, lambda: False, max_ticks=50, tick_interval=0.05)

    # Now link Alice's laptop
    t_ms = now_ms()
    link_invite_id, link_code, _ = invite.create(
        peer_id=alice_phone.peer_id,
        t_ms=t_ms,
        db=alice_phone.db,
        mode='peer',
        user_id=alice_phone.user_id
    )
    alice_phone.db.commit()

    t_ms = now_ms()
    alice_laptop.peer_id = peer.create(t_ms=t_ms, db=alice_laptop.db)
    alice_laptop.db.commit()

    t_ms = now_ms()
    accepted = invite.accept(alice_laptop.peer_id, link_code, t_ms=t_ms, db=alice_laptop.db)
    alice_laptop.db.commit()

    t_ms = now_ms()
    link_result = peer_shared.join(
        peer_id=alice_laptop.peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=accepted['user_id'],
        prekey_id=accepted['invite_prekey_id'],
        t_ms=t_ms,
        db=alice_laptop.db
    )
    alice_laptop.peer_shared_id = link_result['peer_shared_id']
    alice_laptop.user_id = link_result['user_id']
    alice_laptop.channel_id = alice_phone.channel_id
    alice_laptop.db.commit()

    # Wait for laptop to sync the channel first
    from events.content import channel
    def laptop_has_channel():
        channels = channel.list(alice_laptop.peer_id, alice_laptop.db)
        return any(c['channel_id'] == alice_phone.channel_id for c in channels)

    ticks = tick_until(device_link_clients, laptop_has_channel, max_ticks=150)
    assert ticks is not None, "Laptop didn't sync the channel"

    # Wait for laptop to receive all historical messages
    def laptop_has_all_history():
        laptop_msgs = message.list(alice_phone.channel_id, alice_laptop.peer_id, alice_laptop.db)
        laptop_ids = {m['message_id'] for m in laptop_msgs}
        return all(msg_id in laptop_ids for msg_id in historical_msgs)

    ticks = tick_until(device_link_clients, laptop_has_all_history, max_ticks=200)
    assert ticks is not None, "Laptop didn't receive message history"

    # Verify all messages present
    laptop_msgs = message.list(alice_phone.channel_id, alice_laptop.peer_id, alice_laptop.db)
    laptop_contents = {m['content'] for m in laptop_msgs}

    for i in range(3):
        assert f"Historical message {i+1}" in laptop_contents, f"Missing historical message {i+1}"
