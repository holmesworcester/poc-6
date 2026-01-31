"""
Scenario test: Alice adds Bob as admin and both peers converge on Bob's admin status.

Alice creates a network. Bob joins Alice's network via invite.
Alice then adds Bob as an admin using admin events.

Tests:
- Alice is automatically an admin during network creation (via admin event)
- Bob joins as regular user (not admin)
- Alice can add Bob as admin using admin.create()
- Both Alice and Bob converge on Bob's admin status after sync
- Admin-only invite creation enforcement
- Rogue non-admin invites are rejected
"""
from core.db import create_safe_db, create_unsafe_db
from events.identity import user, invite, network as network_module, peer, admin
from core import crypto, store
from tests.utils.tick_helper import assert_eventually


def test_admin_group_workflow(fresh_db):
    """Test admin workflow: Alice creates network, Bob joins, Alice makes Bob admin."""
    from tests.utils.tick_helper import TestClock
    db = fresh_db
    clock = TestClock()  # Manages timestamps

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)

    # Alice creates an invite for Bob
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Verify Alice is admin (network creator)
    assert admin.is_user_admin(alice['user_id'], alice['network_id'], alice['peer_id'], db), \
        "Alice should be admin (network creator)"

    # Wait for Bob to see Alice as admin
    def bob_sees_alice_admin():
        assert admin.is_user_admin(alice['user_id'], alice['network_id'], bob['peer_id'], db)

    t_ms = assert_eventually(bob_sees_alice_admin, db=db, start_t_ms=t_ms)

    # Verify Bob is NOT admin initially
    assert not admin.is_user_admin(bob['user_id'], alice['network_id'], alice['peer_id'], db), \
        "Bob should NOT be admin initially"
    assert not admin.is_user_admin(bob['user_id'], alice['network_id'], bob['peer_id'], db), \
        "Bob should NOT see himself as admin initially"

    # Test: Bob tries to add himself as admin (should fail)
    bob_private_key = peer.get_private_key(bob['peer_id'], bob['peer_id'], db)
    admin.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=bob['peer_shared_id'],
        signer_private_key=bob_private_key,
        t_ms=t_ms,
        peer_id=bob['peer_id'],
        db=db,
        admin_grant=None  # Bob has no admin_grant
    )
    db.commit()

    # Verify Bob is still not admin (his event won't project)
    assert not admin.is_user_admin(bob['user_id'], alice['network_id'], bob['peer_id'], db), \
        "Bob should NOT be admin (his admin event should not project)"

    # Alice adds Bob as admin
    alice_admin_grant = admin.my_grant(alice['user_id'], alice['network_id'], alice['peer_id'], db)
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=t_ms,
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice_admin_grant
    )
    db.commit()

    # Verify Alice sees Bob as admin immediately
    assert admin.is_user_admin(bob['user_id'], alice['network_id'], alice['peer_id'], db), \
        "Alice should see Bob as admin after adding him"

    # Wait for Bob to see himself as admin
    def bob_sees_himself_admin():
        assert admin.is_user_admin(bob['user_id'], alice['network_id'], bob['peer_id'], db)

    t_ms = assert_eventually(bob_sees_himself_admin, db=db, start_t_ms=t_ms)

    # Now Bob (as admin) invites Charlie
    _, charlie_invite_link, _ = invite.create(peer_id=bob['peer_id'], t_ms=t_ms, db=db)

    # Charlie joins via Bob's invite
    charlie_peer_id = peer.create(t_ms=t_ms + 1000, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite_link, name='Charlie', t_ms=t_ms + 1000, db=db)
    db.commit()

    # Wait for Charlie to see everyone
    def charlie_sees_admins():
        assert admin.is_user_admin(alice['user_id'], alice['network_id'], charlie['peer_id'], db), \
            "Charlie should see Alice as admin"
        assert admin.is_user_admin(bob['user_id'], alice['network_id'], charlie['peer_id'], db), \
            "Charlie should see Bob as admin"
        assert not admin.is_user_admin(charlie['user_id'], alice['network_id'], charlie['peer_id'], db), \
            "Charlie should NOT be admin"

    t_ms = assert_eventually(charlie_sees_admins, db=db, start_t_ms=t_ms + 1000)

    # Wait for Alice to see Charlie as a member
    from events.group import group_member
    all_users_group = network_module.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)

    def alice_sees_charlie():
        members = group_member.list_members(all_users_group, alice['peer_id'], db)
        member_names = [m['name'] for m in members]
        assert 'Charlie' in member_names, f"Alice should see Charlie as member, got: {member_names}"

    t_ms = assert_eventually(alice_sees_charlie, db=db, start_t_ms=t_ms)

    # Test: Rogue non-admin invite (Charlie tries to invite Dave before being admin)
    try:
        invite.create(peer_id=charlie['peer_id'], t_ms=t_ms, db=db)
        assert False, "Charlie should NOT be able to create invite (not admin)"
    except ValueError as e:
        assert "not an admin" in str(e).lower(), "Error should mention admin requirement"

    # Now craft a ROGUE invite: manually create an invite event bypassing validation
    charlie_safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
    unsafedb = create_unsafe_db(db)

    charlie_network = charlie_safedb.query_one(
        "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (charlie['peer_id'],)
    )

    all_users_group_id = network_module.get_all_users_group_id(
        charlie_network['network_id'],
        charlie['peer_id'],
        db
    )

    charlie_channel = charlie_safedb.query_one(
        "SELECT channel_id FROM channels WHERE recorded_by = ? AND is_main = 1 LIMIT 1",
        (charlie['peer_id'],)
    )

    # In sender key model, groups don't have key_id - invites pass None
    rogue_invite_private_key, rogue_invite_public_key = crypto.generate_keypair()
    rogue_invite_pubkey_b64 = crypto.b64encode(rogue_invite_public_key)
    rogue_invite_pubkey_id = crypto.b64encode(crypto.hash(rogue_invite_public_key))

    charlie_private_key = peer.get_private_key(charlie['peer_id'], charlie['peer_id'], db)
    rogue_invite_blob = invite.encode_wire_event(
        mode="user",
        invite_pubkey_b64=rogue_invite_pubkey_b64,
        invite_pubkey_id_b64=rogue_invite_pubkey_id,
        group_id_b64=all_users_group_id,
        channel_id_b64=charlie_channel['channel_id'],
        key_id_b64=None,  # Sender key model: no group keys
        network_id_b64=charlie_network['network_id'],
        inviter_peer_shared_id_b64=charlie['peer_shared_id'],
        inviter_user_id_b64=charlie['user_id'],  # Charlie is NOT an admin!
        target_user_id_b64=None,
        admin_grant_id_b64=None,
        inviter_ip=None,
        inviter_port=None,
        signed_by_b64=charlie['peer_shared_id'],
        signer_type='peer_shared',
        created_at_ms=t_ms + 2000,
        private_key=charlie_private_key,
    )
    rogue_invite_id = store.event(rogue_invite_blob, charlie['peer_id'], t_ms + 2000, db)
    db.commit()

    # Wait for sync and verify Alice rejected the rogue invite
    def alice_rejected_rogue():
        alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_rogue_invite = alice_safedb.query_one(
            "SELECT 1 FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
            (rogue_invite_id, alice['peer_id'])
        )
        assert alice_rogue_invite is None, "Alice should reject rogue invite from non-admin Charlie"

        bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
        bob_rogue_invite = bob_safedb.query_one(
            "SELECT 1 FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
            (rogue_invite_id, bob['peer_id'])
        )
        assert bob_rogue_invite is None, "Bob should reject rogue invite from non-admin Charlie"

    assert_eventually(alice_rejected_rogue, db=db, start_t_ms=t_ms + 2000)
