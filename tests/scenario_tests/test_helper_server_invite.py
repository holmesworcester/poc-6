"""Scenario tests for helper server invites (no group access)."""
import sqlite3

from core.db import Database
from core import schema, tick
from events.identity import user, invite, peer, network
from events.content import message
from events.group import group_key, group_member
from tests.utils.tick_helper import run_ticks


def test_helper_invite_skips_group_membership_and_keys():
    """Helper invite should not add server to all_users or share group keys."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    tick.reset_state(db)

    alice = user.new_network(name="Alice", t_ms=1000, db=db)

    _, invite_link, _ = invite.create(
        peer_id=alice["peer_id"],
        t_ms=2000,
        db=db,
        include_group=False,
        share_group_keys=False
    )

    server_peer_id = peer.create(t_ms=3000, db=db)
    server_join = user.join(
        peer_id=server_peer_id,
        invite_link=invite_link,
        name="helper",
        t_ms=4000,
        db=db,
        device_name="server"
    )
    db.commit()

    # Run ticks to allow any pending projections
    run_ticks(db=db, start_t_ms=5000, num_rounds=20)

    all_users_group_id = network.get_all_users_group_id(alice["network_id"], alice["peer_id"], db)
    members = group_member.list_members(all_users_group_id, alice["peer_id"], db)
    member_user_ids = {row["user_id"] for row in members}

    assert server_join["user_id"] not in member_user_ids, "Helper server should not be in all_users group"
    assert group_key.list(server_peer_id, db) == [], "Helper server should not receive group keys"


def test_helper_server_cannot_decrypt_messages():
    """Helper server should not decrypt messages after sync ticks."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    tick.reset_state(db)

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_peer_id = alice["peer_id"]
    alice_channel_id = alice["channel_id"]

    _, invite_link, _ = invite.create(
        peer_id=alice_peer_id,
        t_ms=2000,
        db=db,
        include_group=False,
        share_group_keys=False
    )

    server_peer_id = peer.create(t_ms=3000, db=db)
    user.join(
        peer_id=server_peer_id,
        invite_link=invite_link,
        name="helper",
        t_ms=4000,
        db=db,
        device_name="server"
    )

    # Alice sends a message
    message.create(
        peer_id=alice_peer_id,
        channel_id=alice_channel_id,
        content="hello helpers",
        t_ms=5000,
        db=db
    )
    db.commit()

    # Run ticks to process events
    run_ticks(db=db, start_t_ms=6000, num_rounds=30)

    server_messages = message.list(alice_channel_id, server_peer_id, db)
    assert server_messages == [], "Helper server should not decrypt messages"
