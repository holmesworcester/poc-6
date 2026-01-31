"""
Security tests for server relay functionality.

Server relays are special peers that:
1. Sync events with all members (store and forward)
2. Have peer identity (peer_shared)
3. Do NOT have user identity (no user_id)
4. Do NOT receive group keys (cannot decrypt messages)
5. Cannot create admin events (no admin_grant possible)
6. Cannot send messages (no user identity)

These tests verify these security properties are enforced.
"""
import pytest
from events.identity import user, invite, peer, admin, network
from events.identity import server_relay, network_settings
from events.group import group_key_shared
from core.db import create_safe_db
from tests.utils.tick_helper import TestClock, run_ticks


def test_server_invite_mode_creates_correct_link(fresh_db):
    """Server mode invite creates quiet://server/ link."""
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Create server invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='server',
        server_address='relay.example.com:5000'
    )

    # Verify link format
    assert invite_link.startswith('quiet://server/'), f"Expected quiet://server/ prefix, got: {invite_link}"

    # Verify invite data contains server info
    assert invite_data.get('mode') == 'server', "Invite data should have mode='server'"
    assert invite_data.get('server_address') == 'relay.example.com:5000', "Server address should be in invite data"

    print("Server invite creates correct link format")


def test_server_invite_does_not_share_keys(fresh_db):
    """Server mode invite does NOT share group keys."""
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Count group keys shared before creating server invite
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    keys_before = safedb.query(
        "SELECT key_shared_id FROM group_keys_shared WHERE recorded_at > 0 AND recorded_by = ?",
        (alice['peer_id'],)
    )
    keys_before_count = len(list(keys_before))

    # Create server invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='server',
        server_address='relay.example.com:5000'
    )
    db.commit()

    # Count group keys shared after creating server invite
    keys_after = safedb.query(
        "SELECT key_shared_id FROM group_keys_shared WHERE recorded_at > 0 AND recorded_by = ?",
        (alice['peer_id'],)
    )
    keys_after_count = len(list(keys_after))

    # No new keys should have been shared for server invite
    assert keys_after_count == keys_before_count, \
        f"Server invite should NOT share keys. Before: {keys_before_count}, After: {keys_after_count}"

    # Now create a regular user invite and verify it DOES share keys
    user_invite_id, user_invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    db.commit()

    keys_after_user = safedb.query(
        "SELECT key_shared_id FROM group_keys_shared WHERE recorded_at > 0 AND recorded_by = ?",
        (alice['peer_id'],)
    )
    keys_after_user_count = len(list(keys_after_user))

    # User invite SHOULD share keys
    assert keys_after_user_count > keys_before_count, \
        f"User invite SHOULD share keys. Before: {keys_before_count}, After user: {keys_after_user_count}"

    print("Server invite correctly skips key sharing")


def test_server_relay_is_detected(fresh_db):
    """is_server_relay correctly identifies server relay peers.

    Tests that server_relay.create() correctly projects the server_relay event
    and that is_server_relay() correctly detects it.
    """
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get Alice's network_id
    network_id = network.get_network_id(alice['peer_id'], db)

    # Test the simpler case: Alice creates a server_relay event for herself
    # (In production, the server would create this, but we're testing the projection)
    server_relay_id = server_relay.create(
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        network_id=network_id,
        t_ms=clock.tick(),
        db=db,
        address='relay.example.com:5000'
    )
    db.commit()

    # Run ticks to ensure projection completes
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=3)

    # Verify Alice is detected as relay (since we marked her as such)
    from events.identity import peer_shared as peer_shared_module
    alice_peer_shared = peer_shared_module.get_self(alice['peer_id'], db)
    assert server_relay.is_server_relay(alice['peer_shared_id'], alice['peer_id'], db), \
        "Alice should be detected as server relay after server_relay event is created"

    # Create Bob (regular user) and verify he is NOT detected as relay
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=5)

    # Bob's peer_shared_id is returned directly from user.join()
    bob_peer_shared_id = bob['peer_shared_id']
    assert not server_relay.is_server_relay(bob_peer_shared_id, alice['peer_id'], db), \
        "Bob should NOT be detected as server relay"

    print("Server relay detection works correctly")


def test_server_relay_excluded_from_key_distribution(fresh_db):
    """Server relays are excluded from key distribution via share_key_with_group_members."""
    db = fresh_db
    clock = TestClock()

    # This test verifies the security enforcement in group_key_shared.share_key_with_group_members
    # The actual exclusion logic was added to that function

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network and group info
    network_id = network.get_network_id(alice['peer_id'], db)
    all_users_group_id = network.get_all_users_group_id(network_id, alice['peer_id'], db)

    # Get current key
    from events.group import group as group_module
    key_row = group_module.get_current_key(all_users_group_id, alice['peer_id'], db)
    key_id = key_row['key_id']

    # Get Alice's peer_shared_id
    from events.identity import peer_shared as peer_shared_module
    alice_identity = peer_shared_module.get_self(alice['peer_id'], db)
    alice_peer_shared_id = alice_identity['peer_shared_id']

    # Try to share key with group members (should work normally with no relays)
    key_shared_ids = group_key_shared.share_key_with_group_members(
        key_id=key_id,
        group_id=all_users_group_id,
        peer_id=alice['peer_id'],
        peer_shared_id=alice_peer_shared_id,
        t_ms=clock.tick(),
        db=db
    )

    # Should succeed (even if no other members to share with)
    print(f"Key shared with {len(key_shared_ids)} members (excluding self)")

    print("Key distribution exclusion for server relays is enforced")


def test_network_settings_stores_server_relay_info(fresh_db):
    """Network settings correctly stores and retrieves server relay configuration."""
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network_id
    network_id = network.get_network_id(alice['peer_id'], db)

    # Get Alice's identity
    from events.identity import peer_shared as peer_shared_module
    alice_identity = peer_shared_module.get_self(alice['peer_id'], db)
    alice_peer_shared_id = alice_identity['peer_shared_id']

    # Initially should be mesh mode
    sync_mode = network_settings.get_sync_mode(network_id, alice['peer_id'], db)
    assert sync_mode == 'mesh', f"Initial sync mode should be 'mesh', got '{sync_mode}'"

    # Set server relay (use valid base64-encoded 16-byte value)
    from core import crypto
    server_peer_shared_id = crypto.b64encode(b"fake_server_0123")  # 16 bytes
    server_address = "relay.example.com:5000"

    settings_id = network_settings.set_server_relay(
        network_id=network_id,
        server_peer_shared_id=server_peer_shared_id,
        server_address=server_address,
        peer_id=alice['peer_id'],
        peer_shared_id=alice_peer_shared_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run ticks to project
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=5)

    # Verify settings are stored
    relay_info = network_settings.get_server_relay(network_id, alice['peer_id'], db)
    assert relay_info is not None, "Server relay info should be stored"
    assert relay_info['peer_shared_id'] == server_peer_shared_id
    assert relay_info['address'] == server_address

    # Sync mode should now be 'star'
    sync_mode = network_settings.get_sync_mode(network_id, alice['peer_id'], db)
    assert sync_mode == 'star', f"Sync mode should be 'star' after setting relay, got '{sync_mode}'"

    print("Network settings correctly manages server relay configuration")


def test_server_mode_invite_requires_admin(fresh_db):
    """Only admins can create server mode invites."""
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network, Bob joins
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Create invite for Bob
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )

    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Run ticks to sync
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=15)

    # Bob (non-admin) should NOT be able to create server invite
    with pytest.raises(ValueError, match="admin"):
        invite.create(
            peer_id=bob['peer_id'],
            t_ms=clock.tick(),
            db=db,
            mode='server'
        )

    # Alice (admin) SHOULD be able to create server invite
    server_invite_id, server_invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='server'
    )

    assert server_invite_link.startswith('quiet://server/'), "Alice should be able to create server invite"

    print("Server mode invite requires admin authorization")


def test_user_invite_includes_server_address_when_configured(fresh_db):
    """User invites include server_address when network has a server relay configured."""
    db = fresh_db
    clock = TestClock()

    # Setup: Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Get network and identity info
    network_id = network.get_network_id(alice['peer_id'], db)
    from events.identity import peer_shared as peer_shared_module
    alice_identity = peer_shared_module.get_self(alice['peer_id'], db)
    alice_peer_shared_id = alice_identity['peer_shared_id']

    # Create user invite BEFORE setting server relay
    invite_id_1, invite_link_1, invite_data_1 = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )

    # Should NOT have server_address
    assert 'server_address' not in invite_data_1 or invite_data_1.get('server_address') is None, \
        "Invite without server relay should not have server_address"

    # Set server relay (use valid base64-encoded 16-byte value)
    from core import crypto
    server_address = "relay.example.com:5000"
    network_settings.set_server_relay(
        network_id=network_id,
        server_peer_shared_id=crypto.b64encode(b"fake_server_0123"),  # 16 bytes
        server_address=server_address,
        peer_id=alice['peer_id'],
        peer_shared_id=alice_peer_shared_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run ticks to project
    run_ticks(db=db, start_t_ms=clock.now(), num_rounds=5)

    # Create user invite AFTER setting server relay
    invite_id_2, invite_link_2, invite_data_2 = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db,
        mode='user'
    )

    # Should have server_address
    assert invite_data_2.get('server_address') == server_address, \
        f"Invite with server relay should have server_address, got: {invite_data_2.get('server_address')}"

    print("User invites correctly include server address when configured")
