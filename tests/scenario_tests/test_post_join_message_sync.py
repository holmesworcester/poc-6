"""
Scenario test: Messages sent after join should sync to new members.

Bug: When Alice sends messages AFTER Bob joins, Bob doesn't see them.

Steps to reproduce:
1. Alice creates network, sends 2 messages with attachments
2. Bob joins via invite
3. Sync - Bob sees Alice's 2 messages (OK)
4. Alice sends a 3rd message (plain text, no attachment)
5. Sync - Bob should see the 3rd message but doesn't (BUG)

The pattern seems to be:
- Messages created BEFORE Bob joins sync fine
- Messages created AFTER Bob joins don't sync to Bob
"""
import pytest
import os
from events.identity import user, invite, peer
from events.content import message, message_attachment
from tests.utils import tick_helper


def test_post_join_message_sync_with_prior_attachments(fresh_db):
    """Messages sent after a user joins should sync to them.

    Regression test for bug where messages with attachments before Bob joined
    caused subsequent plain-text messages to not sync due to oversized
    negentropy response packets and sync skip optimization.
    """
    db = fresh_db

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice sends message with attachment #1
    msg1 = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='hey!',
        t_ms=2000,
        db=db
    )
    file_data1 = os.urandom(200 * 1024)  # 200KB
    message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg1['id'],
        file_data=file_data1,
        filename='image1.bin',
        mime_type='application/octet-stream',
        t_ms=2100,
        db=db
    )

    # Initial sync
    t_ms = tick_helper.initial_sync(db, start_t_ms=3000)

    # Alice creates invite
    _, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )

    # Alice sends message with attachment #2
    t_ms += 100
    msg2 = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='second image!',
        t_ms=t_ms,
        db=db
    )
    file_data2 = os.urandom(200 * 1024)
    message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg2['id'],
        file_data=file_data2,
        filename='image2.bin',
        mime_type='application/octet-stream',
        t_ms=t_ms + 100,
        db=db
    )

    # Sync after second message
    t_ms = tick_helper.run_ticks(db, t_ms + 200, 100)

    # Bob joins
    bob_peer_id = peer.create(t_ms=t_ms, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=t_ms + 100, db=db)

    # Sync after Bob joins
    t_ms = tick_helper.run_ticks(db, t_ms + 200, 100)

    # Verify Bob sees the first 2 messages
    bob_messages_before = message.list(alice['channel_id'], bob['peer_id'], db)
    assert len(bob_messages_before) == 2, f"Bob should see 2 messages before Alice's 3rd, got {len(bob_messages_before)}"

    # Alice sends 3rd message (plain text, no attachment)
    t_ms += 100
    message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='hey!',
        t_ms=t_ms,
        db=db
    )

    # Alice sees 3 messages
    alice_messages = message.list(alice['channel_id'], alice['peer_id'], db)
    assert len(alice_messages) == 3, f"Alice should see 3 messages, got {len(alice_messages)}"

    # Sync after 3rd message
    t_ms = tick_helper.run_ticks(db, t_ms + 100, 100)

    # Bob should see 3 messages now (THIS IS THE BUG - Bob only sees 2)
    bob_messages_after = message.list(alice['channel_id'], bob['peer_id'], db)
    assert len(bob_messages_after) == 3, \
        f"Bug: Bob should see 3 messages after sync, but only sees {len(bob_messages_after)}"

    # Verify content
    bob_contents = [m['content'] for m in bob_messages_after]
    assert bob_contents.count('hey!') == 2, \
        f"Bob should see 'hey!' twice (messages 1 and 3), got {bob_contents}"


def test_post_join_plain_message_sync(fresh_db):
    """Plain text message after join should sync (works correctly)."""
    db = fresh_db

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates invite
    _, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Initial sync
    t_ms = tick_helper.run_ticks(db, 3000, 100)

    # Alice sends a message AFTER Bob has joined
    t_ms += 100
    message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='hello bob!',
        t_ms=t_ms,
        db=db
    )

    # Alice sees her message
    alice_messages = message.list(alice['channel_id'], alice['peer_id'], db)
    assert len(alice_messages) == 1

    # Sync
    t_ms = tick_helper.run_ticks(db, t_ms + 100, 100)

    # Bob should see Alice's message (THIS IS THE BUG)
    bob_messages = message.list(alice['channel_id'], bob['peer_id'], db)
    assert len(bob_messages) == 1, \
        f"Bug: Bob should see 1 message from Alice, but sees {len(bob_messages)}"
    assert bob_messages[0]['content'] == 'hello bob!'
