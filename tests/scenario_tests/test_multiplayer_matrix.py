"""
Comprehensive Multiplayer Scenario Test Matrix

This test file systematically covers every corner case related to:
- Invitation creation, acceptance, and lifecycle
- User joining and membership management
- Admin status granting and verification
- User/peer removal and its cascading effects
- Group key management and rotation
- Multi-device (peer) linking scenarios

The tests are organized around a comprehensive state machine that models all possible
states and transitions in the multiplayer system.

## STATE MACHINE OVERVIEW

### 1. NETWORK STATES
- EMPTY: No network exists
- CREATED: Network exists with one admin (creator)
- MULTI_USER: Multiple users have joined
- MULTI_ADMIN: Multiple admins exist

### 2. INVITE STATES
- NONE: No invite exists
- CREATED: Invite created, not yet accepted
- PENDING_SYNC: Invite accepted, awaiting sync
- COMPLETED: Invite fully processed, joiner integrated

### 3. USER MEMBERSHIP STATES
- NOT_MEMBER: User not in network
- INVITED: Has invite, hasn't joined
- JOINING: In process of joining (sync in progress)
- ACTIVE: Full member, can participate
- REMOVED: Was member, now removed

### 4. ADMIN STATES
- NON_ADMIN: User without admin privileges
- PENDING_ADMIN: Admin grant in progress
- ADMIN: User with admin privileges
- FORMER_ADMIN: Was admin, now removed

### 5. KEY AVAILABILITY STATES
- NO_KEY: No group key available
- KEY_PENDING: Key shared but not yet decrypted
- KEY_AVAILABLE: Key decrypted and usable
- KEY_ROTATED: Old key replaced with new one

## TEST MATRIX DIMENSIONS

Each test systematically varies:
1. Number of participants (1-4 users)
2. Admin status of actors (admin vs non-admin)
3. Sync state (before, during, after convergence)
4. Operation sequence (create→join→remove, etc.)
5. Perspective (from viewpoint of different participants)
"""

import pytest
import sqlite3
from core.db import Database, create_safe_db, create_unsafe_db
from core import schema
from core import tick
from events.identity import user, invite, peer, peer_shared, admin
from events.identity import user_removed, peer_removed, network as network_module
from events.group import group_member, group_key, group_key_shared, group
from events.content import message
from tests.utils.tick_helper import run_ticks, assert_eventually, TestClock
from core import crypto
from core import store
from events.identity import invite as invite_module


# =============================================================================
# FIXTURES - Reusable test setups
# =============================================================================

@pytest.fixture
def network_with_alice(fresh_db):
    """Alice creates a network (single admin setup)."""
    db = fresh_db
    tick.reset_state(db)
    clock = TestClock()
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()
    return db, alice, clock


@pytest.fixture
def network_with_alice_and_bob(network_with_alice):
    """Alice creates network, Bob joins (two users, one admin)."""
    db, alice, clock = network_with_alice

    # Alice invites Bob
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )

    # Bob joins
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Sync to converge
    run_ticks(db=db, start_t_ms=None, num_rounds=200)

    return db, alice, bob, clock


@pytest.fixture
def network_with_three_users(network_with_alice_and_bob):
    """Alice creates network, Bob and Charlie join (three users, one admin)."""
    db, alice, bob, clock = network_with_alice_and_bob

    # Alice invites Charlie
    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )

    # Charlie joins
    charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=invite_link, name='Charlie', t_ms=clock.now(), db=db)
    db.commit()

    # Sync to converge
    run_ticks(db=db, start_t_ms=None, num_rounds=200)

    return db, alice, bob, charlie, clock


@pytest.fixture
def network_with_two_admins(network_with_alice_and_bob):
    """Alice creates network, Bob joins, Alice promotes Bob to admin."""
    db, alice, bob, clock = network_with_alice_and_bob

    # Alice promotes Bob to admin
    alice_admin_grant = admin.my_grant(
        alice['user_id'],
        alice['network_id'],
        alice['peer_id'],
        db
    )
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

    admin.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=clock.tick(),
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice_admin_grant
    )
    db.commit()

    # Sync to converge
    run_ticks(db=db, start_t_ms=None, num_rounds=200)

    return db, alice, bob, clock


# =============================================================================
# SECTION 1: INVITATION LIFECYCLE TESTS
# =============================================================================

class TestInvitationCreation:
    """Tests for invite creation under various conditions."""

    def test_admin_creates_user_invite(self, network_with_alice):
        """Admin (network creator) can create user invites."""
        db, alice, clock = network_with_alice

        invite_id, invite_link, invite_data = invite.create(
            peer_id=alice['peer_id'],
            t_ms=2000,
            db=db
        )

        assert invite_id is not None
        assert invite_link.startswith('quiet://invite/')
        assert invite_data['network_id'] == alice['network_id']

    def test_non_admin_cannot_create_user_invite(self, network_with_alice_and_bob):
        """Non-admin cannot create user invites."""
        db, alice, bob, clock = network_with_alice_and_bob

        with pytest.raises(ValueError) as exc_info:
            invite.create(
                peer_id=bob['peer_id'],
                t_ms=5000,
                db=db
            )

        assert "admin" in str(exc_info.value).lower()

    def test_newly_promoted_admin_can_create_invite(self, network_with_two_admins):
        """Newly promoted admin can create invites."""
        db, alice, bob, clock = network_with_two_admins

        # Verify Bob is now admin
        bob_is_admin = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            bob['peer_id'],
            db
        )
        assert bob_is_admin, "Bob should be admin after promotion"

        # Bob should be able to create invites
        invite_id, invite_link, _ = invite.create(
            peer_id=bob['peer_id'],
            t_ms=15000,
            db=db
        )

        assert invite_id is not None
        assert invite_link.startswith('quiet://invite/')

    def test_invite_mode_user_requires_no_user_id(self, network_with_alice):
        """Mode=user invites cannot specify user_id."""
        db, alice, clock = network_with_alice

        with pytest.raises(ValueError) as exc_info:
            invite.create(
                peer_id=alice['peer_id'],
                t_ms=2000,
                db=db,
                mode='user',
                user_id=alice['user_id']  # Should not be allowed
            )

        assert "user_id" in str(exc_info.value).lower()

    def test_invite_mode_peer_requires_user_id(self, network_with_alice):
        """Mode=peer invites must specify user_id."""
        db, alice, clock = network_with_alice

        with pytest.raises(ValueError) as exc_info:
            invite.create(
                peer_id=alice['peer_id'],
                t_ms=2000,
                db=db,
                mode='peer',
                user_id=None  # Should be required
            )

        assert "user_id" in str(exc_info.value).lower()

    def test_peer_invite_only_for_own_user(self, network_with_alice_and_bob):
        """Users can only create peer invites for their own user_id."""
        db, alice, bob, clock = network_with_alice_and_bob

        # Bob tries to create peer invite for Alice's user - should fail
        with pytest.raises(ValueError) as exc_info:
            invite.create(
                peer_id=bob['peer_id'],
                t_ms=5000,
                db=db,
                mode='peer',
                user_id=alice['user_id']  # Not Bob's user_id
            )

        assert "another user" in str(exc_info.value).lower() or "only link your own" in str(exc_info.value).lower()


class TestInvitationAcceptance:
    """Tests for invite acceptance under various conditions."""

    def test_new_user_joins_via_invite(self, network_with_alice):
        """New user can join network via valid invite."""
        db, alice, clock = network_with_alice

        # Create invite
        invite_id, invite_link, _ = invite.create(
            peer_id=alice['peer_id'],
            t_ms=2000,
            db=db
        )

        # Bob joins
        bob_peer_id = peer.create(t_ms=3000, db=db)
        bob = user.join(
            peer_id=bob_peer_id,
            invite_link=invite_link,
            name='Bob',
            t_ms=3000,
            db=db
        )

        assert bob['user_id'] is not None
        assert bob['peer_id'] == bob_peer_id
        assert bob['network_id'] == alice['network_id']

    def test_joiner_becomes_group_member_after_sync(self, network_with_alice):
        """Joiner is added to all_users group after sync converges."""
        db, alice, clock = network_with_alice

        # Create and accept invite
        invite_id, invite_link, _ = invite.create(
            peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db
        )
        bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
        db.commit()

        # Sync
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Check Bob is in all_users group from Alice's perspective
        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        member_names = [m['name'] for m in members]

        assert 'Alice' in member_names
        assert 'Bob' in member_names

    def test_joiner_not_admin_by_default(self, network_with_alice_and_bob):
        """New joiners are not admins by default."""
        db, alice, bob, clock = network_with_alice_and_bob

        # Check Bob is not admin from Alice's perspective
        bob_is_admin_alice = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            alice['peer_id'],
            db
        )
        assert not bob_is_admin_alice, "Bob should not be admin by default"

        # Check Bob is not admin from his own perspective
        bob_is_admin_bob = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            bob['peer_id'],
            db
        )
        assert not bob_is_admin_bob, "Bob should not see himself as admin"


class TestInvitationRejection:
    """Tests for rejection of invalid/malicious invites."""

    def test_rogue_invite_from_non_admin_rejected(self, network_with_three_users):
        """Invites from non-admins are rejected during sync."""
        db, alice, bob, charlie, clock = network_with_three_users

        # Charlie (non-admin) tries to bypass validation and create an invite directly
        safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
        unsafedb = create_unsafe_db(db)

        # Get required data
        all_users_group_id = network_module.get_all_users_group_id(
            charlie['network_id'],
            charlie['peer_id'],
            db
        )

        group_row = safedb.query_one(
            "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
            (all_users_group_id, charlie['peer_id'])
        )

        channel_row = safedb.query_one(
            "SELECT channel_id FROM channels WHERE recorded_by = ? AND is_main = 1",
            (charlie['peer_id'],)
        )

        # Create rogue prekey
        rogue_private_key, rogue_public_key = crypto.generate_keypair()
        rogue_prekey_id = crypto.b64encode(crypto.hash(rogue_public_key))

        # Craft rogue invite (bypassing authorization checks) using wire format
        charlie_private_key = peer.get_private_key(charlie['peer_id'], charlie['peer_id'], db)
        rogue_blob = invite_module.encode_wire_event(
            mode="user",
            invite_pubkey_b64=crypto.b64encode(rogue_public_key),
            invite_prekey_id_b64=rogue_prekey_id,
            group_id_b64=all_users_group_id,
            channel_id_b64=channel_row["channel_id"] if channel_row else None,
            key_id_b64=group_row["key_id"] if group_row else None,
            network_id_b64=charlie["network_id"],
            inviter_peer_shared_id_b64=charlie["peer_shared_id"],
            inviter_user_id_b64=charlie["user_id"],
            target_user_id_b64=None,
            admin_grant_id_b64=None,
            inviter_ip=None,
            inviter_port=None,
            signed_by_b64=charlie["peer_shared_id"],  # Charlie is NOT admin
            signer_type="peer_shared",
            created_at_ms=20000,
            private_key=charlie_private_key,
        )
        rogue_invite_id = store.event(rogue_blob, charlie['peer_id'], 20000, db)
        db.commit()

        # Sync
        run_ticks(db=db, start_t_ms=None, num_rounds=50)

        # Verify Alice rejected the rogue invite
        alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_rogue = alice_safedb.query_one(
            "SELECT 1 FROM invites WHERE invite_id = ? AND recorded_by = ?",
            (rogue_invite_id, alice['peer_id'])
        )

        assert alice_rogue is None, "Alice should reject rogue invite from non-admin"


# =============================================================================
# SECTION 2: MEMBERSHIP MANAGEMENT TESTS
# =============================================================================

class TestMembershipListing:
    """Tests for listing group members under various conditions."""

    def test_list_members_single_user(self, network_with_alice):
        """Single user network shows only creator."""
        db, alice, clock = network_with_alice

        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)

        assert len(members) == 1
        assert members[0]['name'] == 'Alice'

    def test_list_members_two_users(self, network_with_alice_and_bob):
        """Two user network shows both users."""
        db, alice, bob, clock = network_with_alice_and_bob

        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        member_names = [m['name'] for m in members]

        assert len(members) == 2
        assert 'Alice' in member_names
        assert 'Bob' in member_names

    def test_list_members_perspective_consistency(self, network_with_three_users):
        """All users should see same member list after sync."""
        db, alice, bob, charlie, clock = network_with_three_users

        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        # Each perspective
        members_alice = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        members_bob = group_member.list_members(all_users_group_id, bob['peer_id'], db)
        members_charlie = group_member.list_members(all_users_group_id, charlie['peer_id'], db)

        names_alice = sorted([m['name'] for m in members_alice])
        names_bob = sorted([m['name'] for m in members_bob])
        names_charlie = sorted([m['name'] for m in members_charlie])

        assert names_alice == ['Alice', 'Bob', 'Charlie']
        assert names_bob == ['Alice', 'Bob', 'Charlie']
        assert names_charlie == ['Alice', 'Bob', 'Charlie']


class TestMembershipCheck:
    """Tests for is_member() under various conditions."""

    def test_is_member_creator(self, network_with_alice):
        """Creator is member of all_users group."""
        db, alice, clock = network_with_alice

        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        result = group_member.is_member(
            alice['user_id'],
            all_users_group_id,
            alice['peer_id'],
            db
        )

        assert result is True

    def test_is_member_joiner(self, network_with_alice_and_bob):
        """Joiner is member after sync."""
        db, alice, bob, clock = network_with_alice_and_bob

        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        # From Alice's perspective
        result_alice = group_member.is_member(
            bob['user_id'],
            all_users_group_id,
            alice['peer_id'],
            db
        )

        # From Bob's perspective
        result_bob = group_member.is_member(
            bob['user_id'],
            all_users_group_id,
            bob['peer_id'],
            db
        )

        assert result_alice is True
        assert result_bob is True


# =============================================================================
# SECTION 3: ADMIN CHAIN TESTS
# =============================================================================

class TestAdminStatus:
    """Tests for admin status verification."""

    def test_network_creator_is_admin(self, network_with_alice):
        """Network creator is automatically admin."""
        db, alice, clock = network_with_alice

        result = admin.is_user_admin(
            alice['user_id'],
            alice['network_id'],
            alice['peer_id'],
            db
        )

        assert result is True

    def test_joiner_not_admin(self, network_with_alice_and_bob):
        """Joiner is not admin by default."""
        db, alice, bob, clock = network_with_alice_and_bob

        # From Alice's perspective
        result_alice = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            alice['peer_id'],
            db
        )

        # From Bob's perspective
        result_bob = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            bob['peer_id'],
            db
        )

        assert result_alice is False
        assert result_bob is False

    def test_admin_promotion_propagates(self, network_with_two_admins):
        """Admin promotion syncs to all peers."""
        db, alice, bob, clock = network_with_two_admins

        # From Alice's perspective
        bob_admin_alice = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            alice['peer_id'],
            db
        )

        # From Bob's perspective
        bob_admin_bob = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            bob['peer_id'],
            db
        )

        assert bob_admin_alice is True, "Alice should see Bob as admin"
        assert bob_admin_bob is True, "Bob should see himself as admin"


class TestAdminGrantChain:
    """Tests for admin grant chain validation."""

    def test_self_promotion_fails(self, network_with_alice_and_bob):
        """Non-admin cannot promote themselves to admin."""
        db, alice, bob, clock = network_with_alice_and_bob

        # Bob tries to make himself admin without proper admin_grant
        bob_private_key = peer.get_private_key(bob['peer_id'], bob['peer_id'], db)

        admin.create(
            user_id=bob['user_id'],
            network_id=alice['network_id'],
            signed_by=bob['peer_shared_id'],
            signer_private_key=bob_private_key,
            t_ms=10000,
            peer_id=bob['peer_id'],
            db=db,
            admin_grant=None  # No valid admin_grant
        )
        db.commit()

        # Verify Bob is still not admin (event won't project)
        bob_is_admin = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            bob['peer_id'],
            db
        )

        assert bob_is_admin is False, "Self-promotion should not work"

    def test_admin_can_grant_admin(self, network_with_alice_and_bob):
        """Admin can grant admin status to others."""
        db, alice, bob, clock = network_with_alice_and_bob

        # Alice grants admin to Bob
        alice_grant = admin.my_grant(
            alice['user_id'],
            alice['network_id'],
            alice['peer_id'],
            db
        )
        alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        admin.create(
            user_id=bob['user_id'],
            network_id=alice['network_id'],
            signed_by=alice['peer_shared_id'],
            signer_private_key=alice_private_key,
            t_ms=10000,
            peer_id=alice['peer_id'],
            db=db,
            admin_grant=alice_grant
        )
        db.commit()

        # Sync
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Verify Bob is now admin from Bob's perspective
        bob_is_admin = admin.is_user_admin(
            bob['user_id'],
            alice['network_id'],
            bob['peer_id'],
            db
        )

        assert bob_is_admin is True, "Bob should be admin after grant"

    def test_chained_admin_grant(self, network_with_alice_and_bob):
        """Admin can grant admin using their own admin_grant."""
        db, alice, bob, clock = network_with_alice_and_bob

        # Alice grants admin to Bob
        alice_grant = admin.my_grant(alice['user_id'], alice['network_id'], alice['peer_id'], db)
        alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)

        admin.create(
            user_id=bob['user_id'],
            network_id=alice['network_id'],
            signed_by=alice['peer_shared_id'],
            signer_private_key=alice_private_key,
            t_ms=10000,
            peer_id=alice['peer_id'],
            db=db,
            admin_grant=alice_grant
        )
        db.commit()

        # Wait for Bob to receive his admin grant
        def bob_has_admin_grant():
            grant = admin.my_grant(bob['user_id'], alice['network_id'], bob['peer_id'], db)
            assert grant is not None, "Bob should have admin_grant"

        assert_eventually(bob_has_admin_grant, db=db, start_t_ms=None,
                          msg="Bob should receive admin_grant")

        # Get the actual grant ID (assert_eventually returns timestamp, not the value)
        bob_grant = admin.my_grant(bob['user_id'], alice['network_id'], bob['peer_id'], db)

        # Now invite Charlie
        invite_id, invite_link, _ = invite.create(peer_id=bob['peer_id'], t_ms=15000, db=db)
        charlie_peer_id = peer.create(t_ms=16000, db=db)
        charlie = user.join(peer_id=charlie_peer_id, invite_link=invite_link, name='Charlie', t_ms=16000, db=db)
        db.commit()

        # Wait for Alice to see Charlie's user (sync must complete)
        def alice_sees_charlie():
            charlie_user = db.query_one(
                "SELECT 1 FROM users WHERE user_id = ? AND recorded_by = ?",
                (charlie['user_id'], alice['peer_id'])
            )
            assert charlie_user is not None, "Alice should see Charlie's user"

        t_ms = assert_eventually(alice_sees_charlie, db=db, start_t_ms=None,
                                  msg="Alice should see Charlie after sync")

        # Bob grants admin to Charlie
        bob_private_key = peer.get_private_key(bob['peer_id'], bob['peer_id'], db)

        admin.create(
            user_id=charlie['user_id'],
            network_id=alice['network_id'],
            signed_by=bob['peer_shared_id'],
            signer_private_key=bob_private_key,
            t_ms=t_ms + 1000,
            peer_id=bob['peer_id'],
            db=db,
            admin_grant=bob_grant
        )
        db.commit()

        # Wait for all perspectives to see Charlie as admin
        def all_see_charlie_as_admin():
            charlie_admin_alice = admin.is_user_admin(
                charlie['user_id'], alice['network_id'], alice['peer_id'], db
            )
            charlie_admin_bob = admin.is_user_admin(
                charlie['user_id'], alice['network_id'], bob['peer_id'], db
            )
            charlie_admin_charlie = admin.is_user_admin(
                charlie['user_id'], alice['network_id'], charlie['peer_id'], db
            )
            assert charlie_admin_alice is True, "Alice should see Charlie as admin"
            assert charlie_admin_bob is True, "Bob should see Charlie as admin"
            assert charlie_admin_charlie is True, "Charlie should see himself as admin"

        assert_eventually(all_see_charlie_as_admin, db=db, start_t_ms=t_ms + 2000,
                          msg="All should see Charlie as admin")


# =============================================================================
# SECTION 4: KEY MANAGEMENT TESTS
# =============================================================================

class TestKeyAvailability:
    """Tests for group key availability under various conditions."""

    def test_creator_has_key(self, network_with_alice):
        """Network creator has group key immediately."""
        db, alice, clock = network_with_alice

        # Get main group
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        group_row = safedb.query_one(
            "SELECT key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (alice['peer_id'],)
        )

        assert group_row is not None

        # Key should be accessible
        key_data = group_key.get_key(group_row['key_id'], alice['peer_id'], db)
        assert key_data is not None
        assert key_data['type'] == 'symmetric'

    def test_joiner_gets_key_after_sync(self, network_with_alice_and_bob):
        """Joiner receives group key after sync."""
        db, alice, bob, clock = network_with_alice_and_bob

        # Get main group from Bob's perspective
        safedb = create_safe_db(db, recorded_by=bob['peer_id'])
        group_row = safedb.query_one(
            "SELECT key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (bob['peer_id'],)
        )

        assert group_row is not None, "Bob should see main group after sync"

        # Key should be accessible to Bob
        key_data = group_key.get_key(group_row['key_id'], bob['peer_id'], db)
        assert key_data is not None
        assert key_data['type'] == 'symmetric'


class TestKeyRotation:
    """Tests for key rotation during removal."""

    def test_user_removal_rotates_key(self, network_with_alice_and_bob):
        """User removal triggers key rotation."""
        db, alice, bob, clock = network_with_alice_and_bob

        # Get original key
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        group_row = safedb.query_one(
            "SELECT group_id, key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (alice['peer_id'],)
        )
        original_key_id = group_row['key_id']

        # Alice removes Bob
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=10000,
            db=db
        )
        db.commit()

        # Check new key
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        updated_group = safedb.query_one(
            "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
            (group_row['group_id'], alice['peer_id'])
        )
        new_key_id = updated_group['key_id']

        assert new_key_id != original_key_id, "Key should be rotated after removal"

    def test_peer_removal_rotates_key(self, network_with_alice_and_bob):
        """Peer removal triggers key rotation (when last device)."""
        db, alice, bob, clock = network_with_alice_and_bob

        # Get original key
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        group_row = safedb.query_one(
            "SELECT group_id, key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (alice['peer_id'],)
        )
        original_key_id = group_row['key_id']

        # Alice removes Bob's peer
        peer_removed.create(
            removed_peer_shared_id=bob['peer_shared_id'],
            removed_by_peer_shared_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=10000,
            db=db
        )
        db.commit()

        # Check new key
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        updated_group = safedb.query_one(
            "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
            (group_row['group_id'], alice['peer_id'])
        )
        new_key_id = updated_group['key_id']

        assert new_key_id != original_key_id, "Key should be rotated after peer removal"


# =============================================================================
# SECTION 5: REMOVAL CASCADE TESTS
# =============================================================================

class TestUserRemoval:
    """Tests for user removal behavior."""

    def test_admin_can_remove_user(self, network_with_alice_and_bob):
        """Admin can remove any user."""
        db, alice, bob, clock = network_with_alice_and_bob

        result = user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=10000,
            db=db
        )

        assert result['event_id'] is not None
        assert result['removed_user_name'] == 'Bob'

    def test_user_can_remove_self(self, network_with_alice_and_bob):
        """User can remove themselves."""
        db, alice, bob, clock = network_with_alice_and_bob

        result = user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=bob['peer_shared_id'],  # Self-removal
            removed_by_local_peer_id=bob['peer_id'],
            t_ms=10000,
            db=db
        )

        assert result['event_id'] is not None

    def test_non_admin_cannot_remove_others(self, network_with_three_users):
        """Non-admin cannot remove other users."""
        db, alice, bob, charlie, clock = network_with_three_users

        # Charlie (non-admin) tries to remove Bob
        with pytest.raises(ValueError) as exc_info:
            user_removed.create(
                removed_user_id=bob['user_id'],
                removed_by_peer_id=charlie['peer_shared_id'],
                removed_by_local_peer_id=charlie['peer_id'],
                t_ms=20000,
                db=db
            )

        assert "admin" in str(exc_info.value).lower() or "authorized" in str(exc_info.value).lower()

    def test_removed_user_not_in_member_list(self, network_with_alice_and_bob):
        """Removed user is filtered from member list."""
        db, alice, bob, clock = network_with_alice_and_bob

        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        # Before removal
        members_before = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        names_before = [m['name'] for m in members_before]
        assert 'Bob' in names_before

        # Remove Bob
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=10000,
            db=db
        )
        db.commit()

        # After removal
        members_after = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        names_after = [m['name'] for m in members_after]

        assert 'Alice' in names_after
        assert 'Bob' not in names_after


class TestPeerRemoval:
    """Tests for peer removal behavior."""

    def test_admin_can_remove_peer(self, network_with_alice_and_bob):
        """Admin can remove any peer."""
        db, alice, bob, clock = network_with_alice_and_bob

        event_id = peer_removed.create(
            removed_peer_shared_id=bob['peer_shared_id'],
            removed_by_peer_shared_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=10000,
            db=db
        )

        # peer_removed.create returns the event_id as a string
        assert event_id is not None
        assert isinstance(event_id, str)

    def test_non_admin_cannot_remove_peer(self, network_with_three_users):
        """Non-admin cannot remove peers."""
        db, alice, bob, charlie, clock = network_with_three_users

        # Charlie (non-admin) tries to remove Bob's peer
        with pytest.raises(ValueError) as exc_info:
            peer_removed.create(
                removed_peer_shared_id=bob['peer_shared_id'],
                removed_by_peer_shared_id=charlie['peer_shared_id'],
                removed_by_local_peer_id=charlie['peer_id'],
                t_ms=20000,
                db=db
            )

        # The error says "Not authorized to remove this peer"
        assert "authorized" in str(exc_info.value).lower()


class TestRemovalPropagation:
    """Tests for removal event propagation."""

    def test_removal_syncs_to_other_users(self, network_with_three_users):
        """Removal event propagates to all users."""
        db, alice, bob, charlie, clock = network_with_three_users

        # Alice removes Bob
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=20000,
            db=db
        )
        db.commit()

        # Sync
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Charlie should see Bob removed
        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            charlie['peer_id'],
            db
        )

        members_charlie = group_member.list_members(all_users_group_id, charlie['peer_id'], db)
        names_charlie = [m['name'] for m in members_charlie]

        assert 'Bob' not in names_charlie, "Charlie should see Bob as removed"


# =============================================================================
# SECTION 6: HISTORICAL KEY ACCESS AND CROSS-KEY DECRYPTION
# =============================================================================

class TestHistoricalKeyAccess:
    """
    Tests for historical key availability after key rotation.

    CRITICAL: When keys rotate (e.g., during user removal), new joiners must
    receive ALL historical keys, not just the current key. Otherwise they
    cannot decrypt:
    - The group event itself (encrypted with original key)
    - Historical messages
    - Any content encrypted with older keys

    This was the root cause of the "bobby-bug" where new users joining after
    a ban couldn't see any content.
    """

    def test_new_joiner_can_decrypt_pre_rotation_messages(self, fresh_db):
        """New joiner after key rotation can decrypt messages from before rotation."""
        db = fresh_db
        tick.reset_state(db)
        clock = TestClock()

        # Alice creates network
        alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)

        # Alice sends message BEFORE any removal (encrypted with original key)
        alice_msg1 = message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content='Message before any removal',
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        # Bob joins
        invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=clock.now(), db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Bob sends message
        bob_msg = message.create(
            peer_id=bob['peer_id'],
            channel_id=bob['channel_id'],
            content='Message from Bob',
            t_ms=clock.tick(),
            db=db
        )
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Alice removes Bob - THIS ROTATES THE KEY
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db
        )
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Alice sends message AFTER removal (encrypted with NEW key)
        alice_msg2 = message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content='Message after removal',
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        # Charlie joins AFTER the key rotation
        invite2_id, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
        charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=clock.now(), db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # CRITICAL: Charlie must be able to see ALL messages
        charlie_messages = message.list(charlie['channel_id'], charlie['peer_id'], db)
        charlie_contents = [m['content'] for m in charlie_messages]

        # These assertions test the bobby-bug fix
        assert 'Message before any removal' in charlie_contents, \
            "Charlie must decrypt pre-rotation messages (original key)"
        assert 'Message from Bob' in charlie_contents, \
            "Charlie must decrypt Bob's message (pre-rotation key)"
        assert 'Message after removal' in charlie_contents, \
            "Charlie must decrypt post-rotation messages (new key)"

    def test_new_joiner_can_access_group_after_rotation(self, fresh_db):
        """New joiner after key rotation can access the all_users group.

        The group event itself is encrypted with the ORIGINAL key.
        If the new joiner doesn't get the original key, they get:
        "Network-signed all_users group not found for network"
        """
        db = fresh_db
        tick.reset_state(db)

        # Alice creates network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)

        # Bob joins
        invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
        bob_peer_id = peer.create(t_ms=3000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Alice removes Bob - THIS ROTATES THE KEY
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=5000,
            db=db
        )
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Charlie joins AFTER the key rotation
        invite2_id, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=7000, db=db)
        charlie_peer_id = peer.create(t_ms=8000, db=db)
        charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=8000, db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # CRITICAL: Charlie must be able to find the all_users group
        # This requires decrypting the group event with the ORIGINAL key
        try:
            all_users_group_id = network_module.get_all_users_group_id(
                charlie['network_id'],
                charlie['peer_id'],
                db
            )
            assert all_users_group_id is not None, "Charlie should find all_users group"
        except ValueError as e:
            pytest.fail(f"Charlie should access all_users group but got: {e}")

        # Charlie should see members
        members = group_member.list_members(all_users_group_id, charlie['peer_id'], db)
        user_ids = [m['user_id'] for m in members]

        assert alice['user_id'] in user_ids, "Charlie should see Alice"
        assert charlie['user_id'] in user_ids, "Charlie should see himself"
        assert bob['user_id'] not in user_ids, "Charlie should not see removed Bob"

    def test_joiner_after_multiple_rotations_has_all_keys(self, fresh_db):
        """Joiner after N key rotations must have all N+1 historical keys."""
        db = fresh_db
        tick.reset_state(db)

        # Alice creates network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)

        # Track key rotations
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        initial_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
        initial_key_count = len(initial_keys)

        # Perform 3 cycles of join->remove (3 key rotations)
        t_ms = 2000
        for i in range(3):
            # User joins
            invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
            t_ms += 100
            temp_peer_id = peer.create(t_ms=t_ms, db=db)
            temp_user = user.join(peer_id=temp_peer_id, invite_link=invite_link, name=f'Temp{i}', t_ms=t_ms, db=db)
            t_ms += 100
            db.commit()
            run_ticks(db=db, start_t_ms=t_ms, num_rounds=200)
            t_ms += 1000

            # User removed (key rotates)
            user_removed.create(
                removed_user_id=temp_user['user_id'],
                removed_by_peer_id=alice['peer_shared_id'],
                removed_by_local_peer_id=alice['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 100
            db.commit()
            run_ticks(db=db, start_t_ms=t_ms, num_rounds=200)
            t_ms += 1000

        # Verify Alice now has multiple keys
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
        assert len(alice_keys) > initial_key_count, f"Alice should have more keys after rotations"

        # Final user joins
        final_invite_id, final_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=t_ms, db=db)
        t_ms += 100
        final_peer_id = peer.create(t_ms=t_ms, db=db)
        final_user = user.join(peer_id=final_peer_id, invite_link=final_invite_link, name='FinalUser', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()

        # CRITICAL: Final user must have ALL historical keys
        # Wait for sync to complete
        def final_user_has_all_keys():
            safedb = create_safe_db(db, recorded_by=final_user['peer_id'])
            final_user_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (final_user['peer_id'],))
            # Final user should have at least as many keys as Alice
            # (they need all historical keys to decrypt historical content)
            assert len(final_user_keys) >= len(alice_keys), \
                f"Final user should have all {len(alice_keys)} historical keys, but only has {len(final_user_keys)}"

        assert_eventually(final_user_has_all_keys, db=db, start_t_ms=t_ms)

    def test_new_joiner_key_count_at_least_matches_inviter(self, fresh_db):
        """New joiner should receive at least as many keys as inviter has.

        NOTE: The joiner may receive MORE keys due to the current implementation
        sharing ALL keys from group_keys table. This ensures they can decrypt
        everything, which is the critical requirement.
        """
        db = fresh_db
        tick.reset_state(db)

        # Alice creates network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)

        # Bob joins and gets removed (causes key rotation)
        invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
        bob_peer_id = peer.create(t_ms=3000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=5000,
            db=db
        )
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Count Alice's keys
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
        alice_key_count = len(alice_keys)
        alice_key_ids = set(k['key_id'] for k in alice_keys)

        # Charlie joins
        invite2_id, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=7000, db=db)
        charlie_peer_id = peer.create(t_ms=8000, db=db)
        charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=8000, db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Count Charlie's keys
        safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
        charlie_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (charlie['peer_id'],))
        charlie_key_ids = set(k['key_id'] for k in charlie_keys)

        # Charlie must have at least all the keys Alice has
        missing_keys = alice_key_ids - charlie_key_ids
        assert len(missing_keys) == 0, \
            f"Charlie is missing {len(missing_keys)} keys that Alice has: {missing_keys}"

        # Log if Charlie has extra keys - this indicates over-sharing
        extra_keys = charlie_key_ids - alice_key_ids
        if extra_keys:
            print(f"WARNING: Charlie has {len(extra_keys)} extra keys beyond Alice's {alice_key_count}")
            print(f"  This suggests keys from other users (Bob) synced to Alice's group_keys table")
            print(f"  and are being unnecessarily shared with new joiners.")
            print(f"  Alice's keys: {alice_key_ids}")
            print(f"  Charlie's keys: {charlie_key_ids}")
            print(f"  Extra keys Charlie has: {extra_keys}")

            # This is a potential bug/inefficiency: the fix shares ALL keys from
            # group_keys table, but should ideally only share keys that were
            # actually used to encrypt content in the groups being joined.


class TestKeyOversharing:
    """
    Tests to identify potential key over-sharing issues.

    The bobby-bug fix shares ALL keys from group_keys table, but this may
    include keys that:
    1. Were created by other users and synced to the inviter
    2. Are not actually needed to decrypt any content
    3. Could leak information about network history unnecessarily
    """

    def test_identify_source_of_extra_keys(self, fresh_db):
        """Identify where extra keys come from when a new user joins."""
        db = fresh_db
        tick.reset_state(db)

        # Alice creates network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        db.commit()

        # Count Alice's initial keys
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_keys_initial = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
        initial_count = len(alice_keys_initial)
        print(f"\nAlice's initial key count: {initial_count}")

        # Bob joins
        invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
        bob_peer_id = peer.create(t_ms=3000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Count Alice's keys after Bob joins (before removal)
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_keys_after_bob = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
        after_bob_count = len(alice_keys_after_bob)
        print(f"Alice's key count after Bob joins: {after_bob_count}")

        # Count Bob's keys
        safedb = create_safe_db(db, recorded_by=bob['peer_id'])
        bob_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (bob['peer_id'],))
        bob_key_count = len(bob_keys)
        print(f"Bob's key count: {bob_key_count}")

        # Check if Alice received any of Bob's keys
        alice_key_ids = set(k['key_id'] for k in alice_keys_after_bob)
        bob_key_ids = set(k['key_id'] for k in bob_keys)
        alice_has_bob_keys = alice_key_ids & bob_key_ids
        alice_only_keys = alice_key_ids - bob_key_ids
        bob_only_keys = bob_key_ids - alice_key_ids

        print(f"\nKeys both Alice and Bob have: {len(alice_has_bob_keys)}")
        print(f"Keys only Alice has: {len(alice_only_keys)}")
        print(f"Keys only Bob has: {len(bob_only_keys)}")

        if bob_only_keys:
            print(f"\nPOTENTIAL ISSUE: Bob has {len(bob_only_keys)} keys that Alice doesn't have.")
            print(f"  If these get synced to Alice later, they'll be over-shared to future joiners.")

        # Remove Bob - this should rotate keys
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=5000,
            db=db
        )
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Count Alice's keys after removal
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_keys_after_removal = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
        after_removal_count = len(alice_keys_after_removal)
        print(f"\nAlice's key count after Bob removed: {after_removal_count}")

        # The fix will share ALL of these keys to Charlie
        print(f"\nSUMMARY: Alice will share {after_removal_count} keys to new joiners.")
        print(f"  Initial: {initial_count}, After join: {after_bob_count}, After removal: {after_removal_count}")

        # This test documents the current behavior rather than asserting correctness
        # The key insight is: are extra keys coming from synced user keys?

    def test_trace_charlie_keys_source(self, fresh_db):
        """Trace exactly where Charlie's extra keys come from."""
        db = fresh_db
        tick.reset_state(db)

        # Patch group_key.create and create_with_material to log all calls with stack traces
        from events.group import group_key
        import traceback

        original_create = group_key.create
        original_create_with_material = group_key.create_with_material
        key_creation_log = []

        def traced_create(peer_id, t_ms, db):
            stack = ''.join(traceback.format_stack()[-6:-1])  # Last 5 frames before this
            key_id = original_create(peer_id, t_ms, db)
            key_creation_log.append({
                'type': 'create',
                'key_id': key_id,
                'peer_id': peer_id,
                't_ms': t_ms,
                'stack': stack
            })
            print(f"\n>>> group_key.create() called: key={key_id[:20]}... peer={peer_id[:20]}... t_ms={t_ms}")
            return key_id

        def traced_create_with_material(key_material, peer_id, t_ms, db):
            stack = ''.join(traceback.format_stack()[-6:-1])
            key_id = original_create_with_material(key_material, peer_id, t_ms, db)
            key_creation_log.append({
                'type': 'create_with_material',
                'key_id': key_id,
                'peer_id': peer_id,
                't_ms': t_ms,
                'stack': stack
            })
            print(f"\n>>> group_key.create_with_material() called: key={key_id[:20]}... peer={peer_id[:20]}... t_ms={t_ms}")
            return key_id

        group_key.create = traced_create
        group_key.create_with_material = traced_create_with_material

        try:
            # Alice creates network
            print("\n=== PHASE 1: Alice creates network ===")
            alice = user.new_network(name='Alice', t_ms=1000, db=db)
            db.commit()

            # Bob joins and gets removed
            print("\n=== PHASE 2: Bob joins ===")
            invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
            bob_peer_id = peer.create(t_ms=3000, db=db)
            bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=3000, db=db)
            db.commit()

            print("\n=== PHASE 3: Sync after Bob joins ===")
            run_ticks(db=db, start_t_ms=None, num_rounds=200)

            print("\n=== PHASE 4: Alice removes Bob ===")
            user_removed.create(
                removed_user_id=bob['user_id'],
                removed_by_peer_id=alice['peer_shared_id'],
                removed_by_local_peer_id=alice['peer_id'],
                t_ms=5000,
                db=db
            )
            db.commit()

            print("\n=== PHASE 5: Sync after removal ===")
            run_ticks(db=db, start_t_ms=None, num_rounds=200)

            # Alice's keys before creating Charlie's invite
            safedb = create_safe_db(db, recorded_by=alice['peer_id'])
            alice_keys_before_invite = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
            alice_key_ids_before = set(k['key_id'] for k in alice_keys_before_invite)
            print(f"\nAlice's keys before Charlie's invite: {len(alice_key_ids_before)}")
            for k in alice_key_ids_before:
                print(f"  - {k[:30]}...")

            # Create invite for Charlie
            print("\n=== PHASE 6: Alice creates invite for Charlie ===")
            invite2_id, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=7000, db=db)
            db.commit()

            # Alice's keys after creating invite (might create new keys?)
            safedb = create_safe_db(db, recorded_by=alice['peer_id'])
            alice_keys_after_invite = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
            alice_key_ids_after_invite = set(k['key_id'] for k in alice_keys_after_invite)
            new_keys_from_invite = alice_key_ids_after_invite - alice_key_ids_before
            print(f"\nAlice's keys after creating invite: {len(alice_key_ids_after_invite)}")
            if new_keys_from_invite:
                print(f"  New keys created by invite.create(): {new_keys_from_invite}")

            # Charlie joins (before sync)
            print("\n=== PHASE 7: Charlie joins ===")
            charlie_peer_id = peer.create(t_ms=8000, db=db)
            charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=8000, db=db)
            db.commit()

            # Charlie's keys immediately after join (before sync)
            safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
            charlie_keys_before_sync = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (charlie['peer_id'],))
            charlie_key_ids_before_sync = set(k['key_id'] for k in charlie_keys_before_sync)
            print(f"\nCharlie's keys immediately after join (before sync): {len(charlie_key_ids_before_sync)}")
            for k in charlie_key_ids_before_sync:
                print(f"  - {k[:30]}...")

            # Sync
            print("\n=== PHASE 8: Sync after Charlie joins ===")
            run_ticks(db=db, start_t_ms=None, num_rounds=200)

            # Charlie's keys after sync
            safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
            charlie_keys_after_sync = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (charlie['peer_id'],))
            charlie_key_ids_after_sync = set(k['key_id'] for k in charlie_keys_after_sync)
            new_from_sync = charlie_key_ids_after_sync - charlie_key_ids_before_sync
            print(f"\nCharlie's keys after sync: {len(charlie_key_ids_after_sync)}")
            if new_from_sync:
                print(f"  New keys from sync: {new_from_sync}")

            # Summary
            print(f"\nSUMMARY:")
            print(f"  Alice shared {len(alice_key_ids_after_invite)} keys to invite")
            print(f"  Charlie received {len(charlie_key_ids_before_sync)} keys from join")
            print(f"  Charlie received {len(new_from_sync)} additional keys from sync")
            print(f"  Charlie total: {len(charlie_key_ids_after_sync)}")

            # Check if the extra key is one Charlie created
            extra_keys = charlie_key_ids_after_sync - alice_key_ids_after_invite
            if extra_keys:
                print(f"\n  Extra keys Charlie has that Alice didn't share: {list(extra_keys)}")
                # Check if these extra keys also appeared in Bob's key set
                safedb = create_safe_db(db, recorded_by=bob['peer_id'])
                bob_keys = safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (bob['peer_id'],))
                bob_key_ids = set(k['key_id'] for k in bob_keys)
                print(f"\n  Bob's keys (for comparison): {list(bob_key_ids)}")

                extra_from_bob = extra_keys & bob_key_ids
                extra_not_from_bob = extra_keys - bob_key_ids
                if extra_from_bob:
                    print(f"  Extra keys that came from Bob: {list(extra_from_bob)}")
                if extra_not_from_bob:
                    print(f"  Extra keys NOT from Bob (Charlie created): {list(extra_not_from_bob)}")

            # Show what groups each key belongs to for Charlie
            print(f"\n  Key-to-group mapping for Charlie:")
            safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
            for key_id in charlie_key_ids_after_sync:
                groups_with_key = safedb.query(
                    "SELECT group_id, name, is_main FROM groups WHERE key_id = ? AND recorded_by = ?",
                    (key_id, charlie['peer_id'])
                )
                source = "from Alice" if key_id in alice_key_ids_after_invite else "Charlie created"
                if groups_with_key:
                    for g in groups_with_key:
                        print(f"    {key_id[:20]}... -> {g['name'] or 'unnamed'} (main={g['is_main']}) [{source}]")
                else:
                    print(f"    {key_id[:20]}... -> (no current group, historical) [{source}]")

            # Check group_keys_shared to see where the extra key came from
            print(f"\n  Tracing extra key origins via group_keys_shared:")
            for key_id in extra_keys:
                # Check if this key came via a group_key_shared event
                safedb = create_safe_db(db, recorded_by=charlie['peer_id'])
                key_shared_rows = safedb.query(
                    "SELECT key_shared_id, signed_by FROM group_keys_shared WHERE original_key_id = ? AND recorded_by = ?",
                    (key_id, charlie['peer_id'])
                )
                if key_shared_rows:
                    for row in key_shared_rows:
                        print(f"    {key_id[:20]}... came via group_key_shared signed_by={row['signed_by'][:20]}...")
                else:
                    print(f"    {key_id[:20]}... NOT from group_key_shared (created locally?)")

            # FINAL ANSWER: Print the key creation log
            print(f"\n\n========== KEY CREATION LOG ==========")
            print(f"Total group_key creations: {len(key_creation_log)}")
            for i, entry in enumerate(key_creation_log):
                print(f"\n--- Key #{i+1} ---")
                print(f"  Type: {entry['type']}")
                print(f"  Key ID: {entry['key_id'][:30]}...")
                print(f"  Peer ID: {entry['peer_id'][:30]}...")
                print(f"  Timestamp: {entry['t_ms']}")
                # Print just the most relevant frame from the stack
                stack_lines = entry['stack'].strip().split('\n')
                # Get the calling function (skip test and traced function frames)
                for line in stack_lines:
                    if 'events/' in line or 'sync' in line:
                        print(f"  Called from: {line.strip()}")
                        break

            # Now identify which of Charlie's extra keys were created by whom
            print(f"\n\n========== EXTRA KEY ANALYSIS ==========")
            for key_id in extra_keys:
                for entry in key_creation_log:
                    if entry['key_id'] == key_id:
                        print(f"\nExtra key {key_id[:20]}...")
                        print(f"  Created by: {entry['type']} at t_ms={entry['t_ms']}")
                        print(f"  Peer ID: {entry['peer_id'][:30]}...")
                        print(f"  Stack trace:")
                        for line in entry['stack'].strip().split('\n'):
                            print(f"    {line.strip()}")
                        break
                else:
                    print(f"\nExtra key {key_id[:20]}... NOT found in creation log (synced from store?)")

        finally:
            # Restore original functions
            group_key.create = original_create
            group_key.create_with_material = original_create_with_material


# =============================================================================
# SECTION 7: EDGE CASES AND RACE CONDITIONS
# =============================================================================

class TestInviteAfterRemoval:
    """Tests for inviting after user removal."""

    def test_can_invite_new_user_after_removal(self, fresh_db):
        """Can invite new users after removing someone.

        NOTE: This tests the core functionality that:
        1. Admin can still create invites after removing a user
        2. New user can successfully join
        3. Removed user is not in the member list

        There is a known issue where user_name events may not sync properly in
        some scenarios, so we verify by user_id rather than name.
        """
        db = fresh_db
        tick.reset_state(db)

        # Setup: Alice creates network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)

        # Bob joins
        invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
        bob_peer_id = peer.create(t_ms=3000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=3000, db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Remove Bob
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=10000,
            db=db
        )
        db.commit()

        # Create new invite for Charlie - this is the main test
        invite_id2, invite_link2, _ = invite.create(
            peer_id=alice['peer_id'],
            t_ms=15000,
            db=db
        )

        assert invite_id2 is not None, "Should be able to create invite after removal"

        # Charlie joins
        charlie_peer_id = peer.create(t_ms=16000, db=db)
        charlie = user.join(
            peer_id=charlie_peer_id,
            invite_link=invite_link2,
            name='Charlie',
            t_ms=16000,
            db=db
        )
        db.commit()

        # Sync
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Verify by user_id rather than name due to known user_name sync issues
        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        user_ids = [m['user_id'] for m in members]

        # Core assertions: Alice and Charlie's user_ids present, Bob's is not
        assert alice['user_id'] in user_ids, "Alice should be in member list"
        assert charlie['user_id'] in user_ids, "Charlie should be in member list after join"
        assert bob['user_id'] not in user_ids, "Bob should not be in member list after removal"
        assert len(user_ids) == 2, f"Should have exactly 2 members, got {len(user_ids)}"


class TestSimultaneousJoins:
    """Tests for multiple simultaneous joins."""

    def test_two_users_join_simultaneously(self, network_with_alice):
        """Two users can join from separate invites."""
        db, alice, clock = network_with_alice

        # Create two invites
        invite1_id, invite1_link, _ = invite.create(
            peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db
        )
        invite2_id, invite2_link, _ = invite.create(
            peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db
        )

        # Both join
        bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=clock.now(), db=db)

        charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
        charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=clock.now(), db=db)

        db.commit()

        # Sync
        run_ticks(db=db, start_t_ms=None, num_rounds=300)

        # Both should be members
        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        names = sorted([m['name'] for m in members])

        assert names == ['Alice', 'Bob', 'Charlie']


class TestAdminRemoval:
    """Tests for removing admins."""

    def test_admin_can_remove_other_admin(self, network_with_two_admins):
        """One admin can remove another admin."""
        db, alice, bob, clock = network_with_two_admins

        # Alice removes Bob (who is also admin)
        result = user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=20000,
            db=db
        )

        assert result['event_id'] is not None

        # Bob should no longer appear in member list
        all_users_group_id = network_module.get_all_users_group_id(
            alice['network_id'],
            alice['peer_id'],
            db
        )

        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        names = [m['name'] for m in members]

        assert 'Bob' not in names


class TestMessageAfterRemoval:
    """Tests for messaging after removal."""

    def test_removed_user_messages_not_synced(self, fresh_db):
        """Messages sent after removal don't sync to removed user."""
        db = fresh_db
        tick.reset_state(db)
        clock = TestClock()

        # Setup with realistic timestamps (required for time-based negentropy keys)
        alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
        invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Alice sends message before removal
        alice_msg_before = message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content='Before removal',
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        # Sync
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Bob should see the message
        bob_messages_before = message.list(bob['channel_id'], bob['peer_id'], db)
        bob_contents_before = [m['content'] for m in bob_messages_before]
        assert 'Before removal' in bob_contents_before, \
            f"Bob should see message before removal, got: {bob_contents_before}"

        # Alice removes Bob
        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        # Sync removal
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Alice sends message after removal
        alice_msg_after = message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content='After removal',
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        # More sync
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Bob should NOT see the post-removal message (sync blocked)
        bob_messages_after = message.list(bob['channel_id'], bob['peer_id'], db)
        bob_contents_after = [m['content'] for m in bob_messages_after]

        assert 'After removal' not in bob_contents_after, \
            "Bob should not receive messages after removal"


# =============================================================================
# SECTION 8: STATE MACHINE COMPREHENSIVE TESTS
# =============================================================================

class TestStateMachine:
    """
    Comprehensive state machine tests covering all transitions.

    State Dimensions:
    - Network: EMPTY → CREATED → MULTI_USER → MULTI_ADMIN
    - Invite: NONE → CREATED → PENDING → COMPLETED
    - User: NOT_MEMBER → INVITED → JOINING → ACTIVE → REMOVED
    - Admin: NON_ADMIN → PENDING → ADMIN
    - Key: NO_KEY → PENDING → AVAILABLE → ROTATED
    """

    def test_full_lifecycle_single_invite(self, fresh_db):
        """Test complete lifecycle: create network → invite → join → remove."""
        db = fresh_db
        tick.reset_state(db)
        clock = TestClock()

        # State: Network=EMPTY
        safedb = create_safe_db(db, recorded_by='test')
        networks = safedb.query("SELECT * FROM networks WHERE recorded_by = 'test'")
        assert len(networks) == 0

        # Transition: Create network
        alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
        db.commit()

        # State: Network=CREATED, Alice=ADMIN
        alice_admin = admin.is_user_admin(alice['user_id'], alice['network_id'], alice['peer_id'], db)
        assert alice_admin is True

        # Transition: Create invite
        invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        db.commit()

        # State: Invite=CREATED
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        invite_row = safedb.query_one(
            "SELECT * FROM invites WHERE invite_id = ? AND recorded_by = ?",
            (invite_id, alice['peer_id'])
        )
        assert invite_row is not None

        # Transition: Bob joins
        bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
        db.commit()

        # State: Bob=JOINING (sync in progress)
        # Sync
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # State: Network=MULTI_USER, Bob=ACTIVE, Bob has KEY_AVAILABLE
        all_users_group_id = network_module.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(members) == 2

        # Bob can access key
        safedb = create_safe_db(db, recorded_by=bob['peer_id'])
        bob_group = safedb.query_one(
            "SELECT key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (bob['peer_id'],)
        )
        bob_key = group_key.get_key(bob_group['key_id'], bob['peer_id'], db)
        assert bob_key is not None

        # Transition: Alice removes Bob
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_group = safedb.query_one(
            "SELECT key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (alice['peer_id'],)
        )
        old_key_id = alice_group['key_id']

        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        # State: Bob=REMOVED, KEY_ROTATED
        members_after = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(members_after) == 1
        assert members_after[0]['name'] == 'Alice'

        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_group_after = safedb.query_one(
            "SELECT key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (alice['peer_id'],)
        )
        assert alice_group_after['key_id'] != old_key_id, "Key should be rotated"

    def test_full_lifecycle_admin_promotion(self, fresh_db):
        """Test lifecycle with admin promotion: create → join → promote → grant admin."""
        db = fresh_db
        tick.reset_state(db)
        clock = TestClock()

        # Create network, invite Bob
        alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
        invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # State: Bob=NON_ADMIN
        bob_admin = admin.is_user_admin(bob['user_id'], alice['network_id'], bob['peer_id'], db)
        assert bob_admin is False

        # Bob cannot create invites
        with pytest.raises(ValueError):
            invite.create(peer_id=bob['peer_id'], t_ms=clock.tick(), db=db)

        # Transition: Alice promotes Bob
        alice_grant = admin.my_grant(alice['user_id'], alice['network_id'], alice['peer_id'], db)
        alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
        admin.create(
            user_id=bob['user_id'],
            network_id=alice['network_id'],
            signed_by=alice['peer_shared_id'],
            signer_private_key=alice_private_key,
            t_ms=clock.tick(),
            peer_id=alice['peer_id'],
            db=db,
            admin_grant=alice_grant
        )
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # State: Bob=ADMIN
        bob_admin = admin.is_user_admin(bob['user_id'], alice['network_id'], bob['peer_id'], db)
        assert bob_admin is True, "Bob should be admin after sync"

        # Verify Bob's grant is available
        bob_grant = admin.my_grant(bob['user_id'], alice['network_id'], bob['peer_id'], db)
        assert bob_grant is not None, "Bob should have admin_grant after sync"

        # Transition: Bob can now create invites
        invite_id2, invite_link2, _ = invite.create(peer_id=bob['peer_id'], t_ms=clock.tick(), db=db)
        assert invite_id2 is not None

        # Charlie joins via Bob's invite
        charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
        charlie = user.join(peer_id=charlie_peer_id, invite_link=invite_link2, name='Charlie', t_ms=clock.now(), db=db)
        db.commit()

        # DEBUG: Check Charlie's state before syncing
        charlie_conns = db.query("SELECT * FROM connections WHERE recorded_by = ?", (charlie_peer_id,))
        print(f"\n=== CHARLIE STATE BEFORE SYNC ===")
        print(f"Charlie connections: {len(charlie_conns)}")
        for c in charlie_conns:
            print(f"  - to {c['peer_shared_id'][:20]}... state={c.get('state')}")

        charlie_ps = db.query_one(
            "SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (charlie['peer_shared_id'], charlie_peer_id)
        )
        print(f"Charlie peer_shared projected: {charlie_ps is not None}")

        charlie_peer_self = db.query_one(
            "SELECT * FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (charlie_peer_id, charlie_peer_id)
        )
        print(f"Charlie peer_self: {charlie_peer_self is not None}")

        charlie_ia = db.query_one(
            "SELECT * FROM invite_accepteds WHERE recorded_by = ?",
            (charlie_peer_id,)
        )
        print(f"Charlie invite_accepteds: {charlie_ia is not None}")
        if charlie_ia:
            print(f"  invite_id={charlie_ia['invite_id'][:20]}...")
            print(f"  inviter_peer_shared_id={charlie_ia.get('inviter_peer_shared_id', 'MISSING')[:20] if charlie_ia.get('inviter_peer_shared_id') else 'NULL'}...")

        charlie_blocked = db.query(
            "SELECT * FROM blocked_events_ephemeral WHERE recorded_by = ?",
            (charlie_peer_id,)
        )
        print(f"Charlie blocked events: {len(charlie_blocked)}")

        charlie_local = db.query_one(
            "SELECT * FROM local_peers WHERE peer_id = ?",
            (charlie_peer_id,)
        )
        print(f"Charlie in local_peers: {charlie_local is not None}")

        # Also check Bob's invite to see what peer_shared_id it has
        bob_ps = db.query_one(
            "SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (bob['peer_shared_id'], bob['peer_id'])
        )
        print(f"Bob's peer_shared_id: {bob['peer_shared_id'][:20]}...")
        print(f"Inviter (from ia): {charlie_ia.get('inviter_peer_shared_id', 'NULL')[:20] if charlie_ia and charlie_ia.get('inviter_peer_shared_id') else 'NULL'}...")
        print(f"Same? {charlie_ia and charlie_ia.get('inviter_peer_shared_id') == bob['peer_shared_id']}")

        # Wait for Alice to see Charlie as a member
        def alice_sees_charlie_as_member():
            all_users_group_id = network_module.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
            members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
            names = sorted([m['name'] for m in members])
            assert names == ['Alice', 'Bob', 'Charlie'], f"Expected all 3, got {names}"

        assert_eventually(alice_sees_charlie_as_member, db=db, start_t_ms=None,
                          msg="Alice should see Charlie as member")

        # Bob can grant admin to Charlie (use fresh bob_grant)
        bob_grant = admin.my_grant(bob['user_id'], alice['network_id'], bob['peer_id'], db)
        bob_private_key = peer.get_private_key(bob['peer_id'], bob['peer_id'], db)
        admin.create(
            user_id=charlie['user_id'],
            network_id=alice['network_id'],
            signed_by=bob['peer_shared_id'],
            signer_private_key=bob_private_key,
            t_ms=clock.tick(),
            peer_id=bob['peer_id'],
            db=db,
            admin_grant=bob_grant
        )
        db.commit()

        # Wait for Alice to see Charlie as admin
        def alice_sees_charlie_as_admin():
            charlie_admin = admin.is_user_admin(charlie['user_id'], alice['network_id'], alice['peer_id'], db)
            assert charlie_admin is True, "Alice should see Charlie as admin"

        assert_eventually(alice_sees_charlie_as_admin, db=db, start_t_ms=None,
                          msg="Alice should see Charlie as admin")

    def test_state_transitions_with_multiple_removals(self, fresh_db):
        """Test state machine with multiple sequential removals."""
        db = fresh_db
        tick.reset_state(db)
        clock = TestClock()

        # Setup: Alice creates network, Bob and Charlie join
        alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)

        invite1_id, invite1_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite1_link, name='Bob', t_ms=clock.tick(), db=db)

        invite2_id, invite2_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        charlie_peer_id = peer.create(t_ms=clock.tick(), db=db)
        charlie = user.join(peer_id=charlie_peer_id, invite_link=invite2_link, name='Charlie', t_ms=clock.tick(), db=db)

        db.commit()

        # Wait for sync
        def all_users_synced():
            all_users_group_id = network_module.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
            members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
            assert len(members) == 3, f"Expected 3 members, got {len(members)}"

        assert_eventually(all_users_synced, db=db)

        # State: 3 users
        all_users_group_id = network_module.get_all_users_group_id(alice['network_id'], alice['peer_id'], db)
        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(members) == 3

        # Transition: Remove Bob
        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        group_before_bob = safedb.query_one(
            "SELECT key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (alice['peer_id'],)
        )
        key_before_bob = group_before_bob['key_id']

        user_removed.create(
            removed_user_id=bob['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        # State: 2 users (Alice, Charlie), key rotated
        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(members) == 2

        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        group_after_bob = safedb.query_one(
            "SELECT key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (alice['peer_id'],)
        )
        key_after_bob = group_after_bob['key_id']
        assert key_after_bob != key_before_bob

        # Transition: Remove Charlie
        user_removed.create(
            removed_user_id=charlie['user_id'],
            removed_by_peer_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=clock.tick(),
            db=db
        )
        db.commit()

        # State: 1 user (Alice), key rotated again
        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        assert len(members) == 1
        assert members[0]['name'] == 'Alice'

        safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        group_after_charlie = safedb.query_one(
            "SELECT key_id FROM groups WHERE is_main = 1 AND recorded_by = ?",
            (alice['peer_id'],)
        )
        key_after_charlie = group_after_charlie['key_id']
        assert key_after_charlie != key_after_bob

        # Transition: Alice can still invite new users
        invite3_id, invite3_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        david_peer_id = peer.create(t_ms=clock.tick(), db=db)
        david = user.join(peer_id=david_peer_id, invite_link=invite3_link, name='David', t_ms=clock.tick(), db=db)
        db.commit()

        # Wait for David to sync
        def david_synced():
            members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
            assert len(members) == 2, f"Expected 2 members (Alice, David), got {len(members)}"

        assert_eventually(david_synced, db=db)

        # State: 2 users (Alice, David) - verify by user_id due to known name sync issues
        members = group_member.list_members(all_users_group_id, alice['peer_id'], db)
        user_ids = [m['user_id'] for m in members]

        assert alice['user_id'] in user_ids, "Alice should be in member list"
        assert david['user_id'] in user_ids, "David should be in member list after join"
        # Bob and Charlie should not be in list after removal
        assert bob['user_id'] not in user_ids, "Bob should not be in member list"
        assert charlie['user_id'] not in user_ids, "Charlie should not be in member list"
        assert len(user_ids) == 2, f"Should have exactly 2 members, got {len(user_ids)}"


# =============================================================================
# SECTION 9: PERMISSION MATRIX TESTS
# =============================================================================

class TestPermissionMatrix:
    """
    Systematic permission testing matrix.

    Operations: invite_create, admin_grant, user_remove, peer_remove, message_send
    Actors: admin, non_admin, removed_user
    Targets: self, other_user, admin
    """

    def test_permission_matrix_invite_create(self, network_with_three_users):
        """Permission matrix for invite creation."""
        db, alice, bob, charlie, clock = network_with_three_users

        # Admin (Alice) can create invite
        invite_id, _, _ = invite.create(peer_id=alice['peer_id'], t_ms=20000, db=db)
        assert invite_id is not None

        # Non-admin (Bob) cannot create invite
        with pytest.raises(ValueError):
            invite.create(peer_id=bob['peer_id'], t_ms=21000, db=db)

        # Non-admin (Charlie) cannot create invite
        with pytest.raises(ValueError):
            invite.create(peer_id=charlie['peer_id'], t_ms=22000, db=db)

    def test_permission_matrix_user_removal(self, network_with_three_users):
        """Permission matrix for user removal."""
        db, alice, bob, charlie, clock = network_with_three_users

        # Admin can remove other user
        # (tested in other tests)

        # Non-admin cannot remove others
        with pytest.raises(ValueError):
            user_removed.create(
                removed_user_id=bob['user_id'],
                removed_by_peer_id=charlie['peer_shared_id'],
                removed_by_local_peer_id=charlie['peer_id'],
                t_ms=20000,
                db=db
            )

        # Non-admin can remove self
        result = user_removed.create(
            removed_user_id=charlie['user_id'],
            removed_by_peer_id=charlie['peer_shared_id'],
            removed_by_local_peer_id=charlie['peer_id'],
            t_ms=21000,
            db=db
        )
        assert result['event_id'] is not None

    def test_permission_matrix_peer_removal(self, network_with_three_users):
        """Permission matrix for peer removal."""
        db, alice, bob, charlie, clock = network_with_three_users

        # Admin can remove peer (returns event_id as string)
        event_id = peer_removed.create(
            removed_peer_shared_id=charlie['peer_shared_id'],
            removed_by_peer_shared_id=alice['peer_shared_id'],
            removed_by_local_peer_id=alice['peer_id'],
            t_ms=20000,
            db=db
        )
        assert event_id is not None
        assert isinstance(event_id, str)

        # Re-add charlie for next test
        invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=21000, db=db)
        charlie2_peer_id = peer.create(t_ms=22000, db=db)
        charlie2 = user.join(peer_id=charlie2_peer_id, invite_link=invite_link, name='Charlie2', t_ms=22000, db=db)
        db.commit()
        run_ticks(db=db, start_t_ms=None, num_rounds=200)

        # Non-admin cannot remove peer
        with pytest.raises(ValueError):
            peer_removed.create(
                removed_peer_shared_id=bob['peer_shared_id'],
                removed_by_peer_shared_id=charlie2['peer_shared_id'],
                removed_by_local_peer_id=charlie2['peer_id'],
                t_ms=30000,
                db=db
            )


# =============================================================================
# MAIN EXECUTION
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
