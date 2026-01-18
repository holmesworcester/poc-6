"""Three player messaging over real UDP.

Tests that Alice, Bob, and Charlie can all exchange messages
with separate databases communicating over real UDP sockets.
"""

import pytest
from events.content import message
from tests.networking.conftest import (
    create_network_and_join, tick_until, now_ms
)


def test_three_player_messaging_real_udp(three_clients):
    """Test three players exchanging messages over real UDP."""
    alice = three_clients["alice"]
    bob = three_clients["bob"]
    charlie = three_clients["charlie"]

    # Set up network with all clients joined
    channel_id = create_network_and_join(three_clients)

    # Run initial ticks to establish connections between peers
    tick_until(three_clients, lambda: False, max_ticks=30, tick_interval=0.05)

    # Alice sends a message
    t_ms = now_ms()
    alice_msg = message.create(
        content="Hello from Alice!",
        channel_id=channel_id,
        peer_id=alice.peer_id,
        t_ms=t_ms,
        db=alice.db
    )
    alice.db.commit()
    alice_msg_id = alice_msg['id']

    # Wait for Bob and Charlie to receive Alice's message
    def bob_and_charlie_have_alice_msg():
        bob_msgs = message.list(channel_id, bob.peer_id, bob.db)
        charlie_msgs = message.list(channel_id, charlie.peer_id, charlie.db)
        bob_has = any(m['message_id'] == alice_msg_id for m in bob_msgs)
        charlie_has = any(m['message_id'] == alice_msg_id for m in charlie_msgs)
        return bob_has and charlie_has

    ticks = tick_until(three_clients, bob_and_charlie_have_alice_msg, max_ticks=100)
    assert ticks is not None, f"Bob/Charlie didn't receive Alice's message"

    # Bob sends a message
    t_ms = now_ms()
    bob_msg = message.create(
        content="Hello from Bob!",
        channel_id=channel_id,
        peer_id=bob.peer_id,
        t_ms=t_ms,
        db=bob.db
    )
    bob.db.commit()
    bob_msg_id = bob_msg['id']

    # Wait for Alice and Charlie to receive Bob's message
    def alice_and_charlie_have_bob_msg():
        alice_msgs = message.list(channel_id, alice.peer_id, alice.db)
        charlie_msgs = message.list(channel_id, charlie.peer_id, charlie.db)
        alice_has = any(m['message_id'] == bob_msg_id for m in alice_msgs)
        charlie_has = any(m['message_id'] == bob_msg_id for m in charlie_msgs)
        return alice_has and charlie_has

    ticks = tick_until(three_clients, alice_and_charlie_have_bob_msg, max_ticks=100)
    assert ticks is not None, f"Alice/Charlie didn't receive Bob's message"

    # Charlie sends a message
    t_ms = now_ms()
    charlie_msg = message.create(
        content="Hello from Charlie!",
        channel_id=channel_id,
        peer_id=charlie.peer_id,
        t_ms=t_ms,
        db=charlie.db
    )
    charlie.db.commit()
    charlie_msg_id = charlie_msg['id']

    # Wait for Alice and Bob to receive Charlie's message
    def alice_and_bob_have_charlie_msg():
        alice_msgs = message.list(channel_id, alice.peer_id, alice.db)
        bob_msgs = message.list(channel_id, bob.peer_id, bob.db)
        alice_has = any(m['message_id'] == charlie_msg_id for m in alice_msgs)
        bob_has = any(m['message_id'] == charlie_msg_id for m in bob_msgs)
        return alice_has and bob_has

    ticks = tick_until(three_clients, alice_and_bob_have_charlie_msg, max_ticks=100)
    assert ticks is not None, f"Alice/Bob didn't receive Charlie's message"

    # Final verification: everyone sees all three messages
    alice_msgs = message.list(channel_id, alice.peer_id, alice.db)
    bob_msgs = message.list(channel_id, bob.peer_id, bob.db)
    charlie_msgs = message.list(channel_id, charlie.peer_id, charlie.db)

    # Extract message contents
    alice_contents = {m['content'] for m in alice_msgs}
    bob_contents = {m['content'] for m in bob_msgs}
    charlie_contents = {m['content'] for m in charlie_msgs}

    expected = {"Hello from Alice!", "Hello from Bob!", "Hello from Charlie!"}

    assert expected.issubset(alice_contents), f"Alice missing messages: {expected - alice_contents}"
    assert expected.issubset(bob_contents), f"Bob missing messages: {expected - bob_contents}"
    assert expected.issubset(charlie_contents), f"Charlie missing messages: {expected - charlie_contents}"


def test_sequential_join_and_message(three_clients):
    """Test that users who join later can sync existing messages."""
    alice = three_clients["alice"]
    bob = three_clients["bob"]
    charlie = three_clients["charlie"]

    # Only Alice creates network initially
    t_ms = now_ms()
    from events.identity import user, invite, peer

    result = user.new_network(name="SequentialNet", t_ms=t_ms, db=alice.db)
    alice.peer_id = result['peer_id']
    alice.peer_shared_id = result['peer_shared_id']
    alice.user_id = result['user_id']
    alice.network_id = result['network_id']
    alice.channel_id = result['channel_id']
    alice.db.commit()

    # Alice sends a message BEFORE others join
    t_ms = now_ms()
    early_msg = message.create(
        content="Message before others joined",
        channel_id=alice.channel_id,
        peer_id=alice.peer_id,
        t_ms=t_ms,
        db=alice.db
    )
    alice.db.commit()

    # Create invite
    t_ms = now_ms()
    invite_id, invite_code, _ = invite.create(
        peer_id=alice.peer_id,
        t_ms=t_ms,
        db=alice.db
    )
    alice.db.commit()

    # Run some ticks for Alice
    for _ in range(5):
        alice.tick(now_ms())

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

    # Sync Alice and Bob
    def bob_has_early_msg():
        bob_msgs = message.list(alice.channel_id, bob.peer_id, bob.db)
        return any(m['content'] == "Message before others joined" for m in bob_msgs)

    # Use just alice and bob for this sync
    two_clients = {"alice": alice, "bob": bob}
    ticks = tick_until(two_clients, bob_has_early_msg, max_ticks=100)
    assert ticks is not None, "Bob didn't receive the early message"

    # Bob sends a message
    t_ms = now_ms()
    bob_msg = message.create(
        content="Bob's message before Charlie",
        channel_id=alice.channel_id,
        peer_id=bob.peer_id,
        t_ms=t_ms,
        db=bob.db
    )
    bob.db.commit()

    # Sync
    tick_until(two_clients, lambda: True, max_ticks=10)

    # Charlie joins
    t_ms = now_ms()
    charlie.peer_id = peer.create(t_ms=t_ms, db=charlie.db)
    charlie.db.commit()

    join_result = user.join(
        peer_id=charlie.peer_id,
        invite_link=invite_code,
        name="Charlie",
        device_name="CharlieDevice",
        t_ms=t_ms,
        db=charlie.db
    )
    charlie.peer_shared_id = join_result['peer_shared_id']
    charlie.user_id = join_result['user_id']
    charlie.network_id = join_result['network_id']
    charlie.channel_id = join_result['channel_id']
    charlie.db.commit()

    # Charlie should eventually sync both messages
    def charlie_has_both_msgs():
        charlie_msgs = message.list(alice.channel_id, charlie.peer_id, charlie.db)
        contents = {m['content'] for m in charlie_msgs}
        return "Message before others joined" in contents and "Bob's message before Charlie" in contents

    ticks = tick_until(three_clients, charlie_has_both_msgs, max_ticks=150)
    assert ticks is not None, "Charlie didn't receive the messages"

    # Final verification
    charlie_msgs = message.list(alice.channel_id, charlie.peer_id, charlie.db)
    contents = {m['content'] for m in charlie_msgs}
    assert "Message before others joined" in contents
    assert "Bob's message before Charlie" in contents
