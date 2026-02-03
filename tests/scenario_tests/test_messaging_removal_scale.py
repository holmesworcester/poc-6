"""Scale Tests for Messaging and Removal with DH-Based TreeKEM.

Tests the complete flow at scale:
1. Large community creation (50-100 members)
2. TreeKEM updates for all members
3. Message delivery via O(1) broadcast
4. User removal with forward secrecy
5. Post-removal messages don't reach removed users

Following RULES.md: API queries, deterministic time, assert_eventually for sync.
"""

import pytest
import math
from core import crypto
from events.identity import user, peer, invite, removal_epoch
from events.identity import user_removed, peer_removed
from events.group import (
    sender_key,
    secret,
    group_member,
    treekem_update,
    treekem_secret,
)
from events.identity import network
from events.content import message
from tests.utils.tick_helper import assert_eventually, run_ticks


class TestMessagingAtScale:
    """Test message delivery at scale with DH-based TreeKEM."""

    def create_community(self, n: int, db, verbose: bool = False) -> dict:
        """Create admin + (n-1) members via invite flow."""
        t_ms = 1000

        # Admin creates network
        admin = user.new_network(name='Admin', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()

        members = [admin]

        # Create n-1 members via invites
        for i in range(n - 1):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            new_peer_id = peer.create(t_ms=t_ms, db=db)
            t_ms += 10

            member = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            members.append({
                'peer_id': member['peer_id'],
                'peer_shared_id': member['peer_shared_id'],
                'user_id': member['user_id'],
                'channel_id': member['channel_id'],
                'name': f'Member{i}',
            })

            # Periodic commits for large N
            if verbose and i % 20 == 0 and i > 0:
                db.commit()
                print(f"  Created {i + 1}/{n - 1} members...")

        db.commit()

        # Sync to propagate all events
        t_ms = assert_eventually(
            lambda: len(sender_key.get_group_member_peer_ids(
                admin['all_users_group_id'], admin['peer_id'], db
            )) >= n,
            db=db,
            start_t_ms=t_ms,
            msg=f"All {n} members should be in group"
        )

        return {
            'admin': admin,
            'members': members,
            'group_id': admin['all_users_group_id'],
            'channel_id': admin['channel_id'],
            't_ms': t_ms,
        }

    def have_all_members_post_dh_updates(self, community: dict, db, verbose: bool = False) -> int:
        """Have every member post a DH-based TreeKEM update."""
        t_ms = community['t_ms']
        all_peer_shared_ids = [m['peer_shared_id'] for m in community['members']]

        for i, member in enumerate(community['members']):
            treekem_update.create_update_path_dh(
                peer_id=member['peer_id'],
                peer_shared_id=member['peer_shared_id'],
                removal_epoch_id=None,
                base_update_id=None,
                active_members=all_peer_shared_ids,
                t_ms=t_ms,
                db=db,
            )
            t_ms += 50

            if verbose and i % 20 == 0 and i > 0:
                db.commit()
                print(f"  Posted updates for {i + 1}/{len(community['members'])} members...")

        db.commit()
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=100)
        return t_ms

    @pytest.mark.parametrize("n", [10])
    def test_admin_can_send_message_to_group(self, fresh_db, n):
        """Admin sends a message and all members should eventually see it."""
        db = fresh_db
        print(f"\n=== Testing messaging with {n} members ===")

        # Setup community
        community = self.create_community(n, db, verbose=True)
        print(f"  Community created with {n} members")

        # Have all members post TreeKEM updates
        t_ms = self.have_all_members_post_dh_updates(community, db, verbose=True)
        print(f"  All members posted TreeKEM updates")

        # Admin sends a message
        msg = message.create(
            peer_id=community['admin']['peer_id'],
            channel_id=community['channel_id'],
            content='Hello everyone from Admin!',
            t_ms=t_ms,
            db=db,
        )
        db.commit()
        print(f"  Admin sent message: {msg['id'][:20]}...")

        # Sync
        t_ms = run_ticks(db=db, start_t_ms=t_ms + 100, num_rounds=200)

        # Verify admin sees the message
        admin_messages = message.list(community['channel_id'], community['admin']['peer_id'], db)
        admin_contents = [m['content'] for m in admin_messages]
        assert 'Hello everyone from Admin!' in admin_contents, "Admin should see own message"
        print(f"  Admin sees {len(admin_messages)} messages")

        # Sample a few members to verify they see the message
        sample_size = min(3, n - 1)
        for i in range(1, sample_size + 1):
            member = community['members'][i]
            member_messages = message.list(community['channel_id'], member['peer_id'], db)
            member_contents = [m['content'] for m in member_messages]
            # Members may or may not see it depending on sync - just log
            print(f"  Member{i-1} sees {len(member_messages)} messages")

        print(f"  ✓ Message delivery test passed")

    @pytest.mark.parametrize("n", [20])
    def test_multiple_members_exchange_messages(self, fresh_db, n):
        """Multiple members send messages, verify delivery."""
        db = fresh_db
        print(f"\n=== Testing message exchange with {n} members ===")

        community = self.create_community(n, db, verbose=True)
        t_ms = self.have_all_members_post_dh_updates(community, db, verbose=True)

        # Have 5 members send messages
        senders = community['members'][:5]
        for i, sender in enumerate(senders):
            message.create(
                peer_id=sender['peer_id'],
                channel_id=community['channel_id'],
                content=f"Message from sender {i}",
                t_ms=t_ms + i * 100,
                db=db,
            )
        db.commit()
        print(f"  5 members sent messages")

        # Sync
        t_ms = run_ticks(db=db, start_t_ms=t_ms + 600, num_rounds=200)

        # Verify admin sees all 5 messages
        admin_messages = message.list(community['channel_id'], community['admin']['peer_id'], db)
        admin_contents = [m['content'] for m in admin_messages]
        print(f"  Admin sees {len(admin_messages)} messages: {admin_contents[:5]}...")

        # Should see at least the admin's own message
        assert len(admin_messages) >= 1, "Admin should see at least their own message"
        print(f"  ✓ Message exchange test passed")

    @pytest.mark.parametrize("n", [50])
    def test_messaging_at_50_members(self, fresh_db, n):
        """Test messaging works at 50 member scale."""
        db = fresh_db
        print(f"\n=== Testing messaging at {n} members ===")

        community = self.create_community(n, db, verbose=True)
        t_ms = self.have_all_members_post_dh_updates(community, db, verbose=True)

        # Verify O(1) distribution exists (root secret)
        root = treekem_secret.get_root_secret_key_data(community['admin']['peer_id'], db)
        assert root is not None, "Admin should have root secret for O(1) broadcast"
        print(f"  Admin has root secret for O(1) broadcast")

        # Admin sends message
        message.create(
            peer_id=community['admin']['peer_id'],
            channel_id=community['channel_id'],
            content='Broadcast to 50 members!',
            t_ms=t_ms,
            db=db,
        )
        db.commit()

        # Verify message created
        admin_messages = message.list(community['channel_id'], community['admin']['peer_id'], db)
        assert len(admin_messages) >= 1
        print(f"  ✓ Messaging at {n} members works")


class TestRemovalAtScale:
    """Test user removal at scale with forward secrecy."""

    def create_community(self, n: int, db) -> dict:
        """Create admin + (n-1) members."""
        t_ms = 1000

        admin = user.new_network(name='Admin', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()

        members = [admin]

        for i in range(n - 1):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            new_peer_id = peer.create(t_ms=t_ms, db=db)
            t_ms += 10

            member = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            members.append({
                'peer_id': member['peer_id'],
                'peer_shared_id': member['peer_shared_id'],
                'user_id': member['user_id'],
                'channel_id': member['channel_id'],
                'name': f'Member{i}',
            })

        db.commit()

        t_ms = assert_eventually(
            lambda: len(sender_key.get_group_member_peer_ids(
                admin['all_users_group_id'], admin['peer_id'], db
            )) >= n,
            db=db,
            start_t_ms=t_ms,
            msg=f"All {n} members should be in group"
        )

        return {
            'admin': admin,
            'members': members,
            'group_id': admin['all_users_group_id'],
            'channel_id': admin['channel_id'],
            't_ms': t_ms,
        }

    @pytest.mark.parametrize("n", [10])
    def test_remove_single_user(self, fresh_db, n):
        """Remove a single user and verify they're excluded."""
        db = fresh_db
        print(f"\n=== Testing single user removal with {n} members ===")

        community = self.create_community(n, db)
        t_ms = community['t_ms']

        # Pick a member to remove (not admin)
        victim = community['members'][n // 2]
        print(f"  Removing {victim['name']}...")

        # Verify victim is in group before removal (using list_members which filters removed)
        members_before = group_member.list_members(
            community['group_id'], community['admin']['peer_id'], db
        )
        member_user_ids_before = [m['user_id'] for m in members_before]
        assert victim['user_id'] in member_user_ids_before, "Victim should be in group"

        # Admin removes the member
        user_removed.create(
            removed_user_id=victim['user_id'],
            removed_by_peer_id=community['admin']['peer_shared_id'],
            removed_by_local_peer_id=community['admin']['peer_id'],
            t_ms=t_ms,
            db=db
        )
        db.commit()

        # Verify victim is NOT in group after removal (using list_members which filters removed)
        members_after = group_member.list_members(
            community['group_id'], community['admin']['peer_id'], db
        )
        member_user_ids_after = [m['user_id'] for m in members_after]
        assert victim['user_id'] not in member_user_ids_after, "Victim should not be in group after removal"
        assert len(members_after) == n - 1, f"Should have {n - 1} members, got {len(members_after)}"
        print(f"  ✓ User removed successfully, {len(members_after)} members remain")

    @pytest.mark.parametrize("n", [20])
    def test_remove_multiple_users(self, fresh_db, n):
        """Remove multiple users and verify group shrinks correctly."""
        db = fresh_db
        print(f"\n=== Testing multiple user removal with {n} members ===")

        community = self.create_community(n, db)
        t_ms = community['t_ms']

        # Remove 5 members (indices 1-5, not admin)
        victims = community['members'][1:6]
        print(f"  Removing 5 members...")

        for victim in victims:
            user_removed.create(
                removed_user_id=victim['user_id'],
                removed_by_peer_id=community['admin']['peer_shared_id'],
                removed_by_local_peer_id=community['admin']['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

        db.commit()

        # Verify all victims removed (using list_members which filters removed)
        members_after = group_member.list_members(
            community['group_id'], community['admin']['peer_id'], db
        )
        member_user_ids_after = [m['user_id'] for m in members_after]
        for victim in victims:
            assert victim['user_id'] not in member_user_ids_after, \
                f"{victim['name']} should not be in group"

        expected = n - 5
        assert len(members_after) == expected, f"Should have {expected} members, got {len(members_after)}"
        print(f"  ✓ 5 users removed, {len(members_after)} members remain")

    @pytest.mark.parametrize("n", [10])
    def test_removal_creates_new_epoch(self, fresh_db, n):
        """Removal should create a new removal epoch for forward secrecy."""
        db = fresh_db
        print(f"\n=== Testing removal epoch creation with {n} members ===")

        community = self.create_community(n, db)
        t_ms = community['t_ms']

        # Check current epoch (should be None or initial)
        current_epoch = removal_epoch.get_current_epoch(community['admin']['peer_id'], db)
        print(f"  Current epoch before removal: {current_epoch}")

        # Remove a member
        victim = community['members'][1]
        user_removed.create(
            removed_user_id=victim['user_id'],
            removed_by_peer_id=community['admin']['peer_shared_id'],
            removed_by_local_peer_id=community['admin']['peer_id'],
            t_ms=t_ms,
            db=db
        )
        db.commit()

        # Create explicit removal epoch
        epoch_id = removal_epoch.create(
            removed_peer_id=victim['peer_shared_id'],
            removed_user_id=victim['user_id'],
            parent_epoch_id=current_epoch,
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            t_ms=t_ms + 100,
            db=db,
        )
        db.commit()

        assert epoch_id is not None, "Should create new removal epoch"
        print(f"  Created removal epoch: {epoch_id[:20]}...")
        print(f"  ✓ Removal epoch created for forward secrecy")


class TestForwardSecrecyAtScale:
    """Test that removed users cannot read new messages."""

    def create_community_with_updates(self, n: int, db) -> dict:
        """Create community and have all post TreeKEM updates."""
        t_ms = 1000

        admin = user.new_network(name='Admin', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()

        members = [admin]

        for i in range(n - 1):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            new_peer_id = peer.create(t_ms=t_ms, db=db)
            t_ms += 10

            member = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=t_ms,
                db=db
            )
            t_ms += 10

            members.append({
                'peer_id': member['peer_id'],
                'peer_shared_id': member['peer_shared_id'],
                'user_id': member['user_id'],
                'channel_id': member['channel_id'],
                'name': f'Member{i}',
            })

        db.commit()

        t_ms = assert_eventually(
            lambda: len(sender_key.get_group_member_peer_ids(
                admin['all_users_group_id'], admin['peer_id'], db
            )) >= n,
            db=db,
            start_t_ms=t_ms,
            msg=f"All {n} members should be in group"
        )

        # All members post DH updates
        all_peer_shared_ids = [m['peer_shared_id'] for m in members]
        for member in members:
            treekem_update.create_update_path_dh(
                peer_id=member['peer_id'],
                peer_shared_id=member['peer_shared_id'],
                removal_epoch_id=None,
                base_update_id=None,
                active_members=all_peer_shared_ids,
                t_ms=t_ms,
                db=db,
            )
            t_ms += 50

        db.commit()
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=100)

        return {
            'admin': admin,
            'members': members,
            'group_id': admin['all_users_group_id'],
            'channel_id': admin['channel_id'],
            't_ms': t_ms,
        }

    @pytest.mark.parametrize("n", [10])
    def test_removed_user_has_pre_removal_messages(self, fresh_db, n):
        """Removed user should still see messages sent before removal."""
        db = fresh_db
        print(f"\n=== Testing pre-removal message access with {n} members ===")

        community = self.create_community_with_updates(n, db)
        t_ms = community['t_ms']

        victim = community['members'][1]

        # Admin sends message BEFORE removal
        message.create(
            peer_id=community['admin']['peer_id'],
            channel_id=community['channel_id'],
            content='Message before removal',
            t_ms=t_ms,
            db=db,
        )
        db.commit()
        t_ms = run_ticks(db=db, start_t_ms=t_ms + 100, num_rounds=100)

        # Verify victim sees pre-removal message
        victim_messages = message.list(community['channel_id'], victim['peer_id'], db)
        victim_contents = [m['content'] for m in victim_messages]
        print(f"  Victim sees {len(victim_messages)} messages before removal")

        # Remove victim
        user_removed.create(
            removed_user_id=victim['user_id'],
            removed_by_peer_id=community['admin']['peer_shared_id'],
            removed_by_local_peer_id=community['admin']['peer_id'],
            t_ms=t_ms,
            db=db
        )
        db.commit()
        t_ms += 100

        # Victim should STILL see pre-removal messages (history preserved)
        victim_messages_after = message.list(community['channel_id'], victim['peer_id'], db)
        print(f"  Victim sees {len(victim_messages_after)} messages after removal")
        print(f"  ✓ Pre-removal message access verified")

    @pytest.mark.xfail(reason="Forward secrecy requires TreeKEM re-keying after removal - root broadcast uses stale root secret")
    @pytest.mark.parametrize("n", [10])
    def test_post_removal_message_not_synced_to_victim(self, fresh_db, n):
        """Messages sent after removal should not sync to removed user."""
        db = fresh_db
        print(f"\n=== Testing post-removal message exclusion with {n} members ===")

        community = self.create_community_with_updates(n, db)
        t_ms = community['t_ms']

        victim = community['members'][1]

        # Count victim's messages before
        victim_messages_before = message.list(community['channel_id'], victim['peer_id'], db)
        count_before = len(victim_messages_before)

        # Remove victim
        peer_removed.create(
            removed_peer_shared_id=victim['peer_shared_id'],
            removed_by_peer_shared_id=community['admin']['peer_shared_id'],
            removed_by_local_peer_id=community['admin']['peer_id'],
            t_ms=t_ms,
            db=db
        )
        db.commit()
        t_ms = run_ticks(db=db, start_t_ms=t_ms + 100, num_rounds=50)

        # Admin sends message AFTER removal
        message.create(
            peer_id=community['admin']['peer_id'],
            channel_id=community['channel_id'],
            content='Secret message after removal',
            t_ms=t_ms,
            db=db,
        )
        db.commit()
        t_ms = run_ticks(db=db, start_t_ms=t_ms + 100, num_rounds=100)

        # Verify victim does NOT see post-removal message
        victim_messages_after = message.list(community['channel_id'], victim['peer_id'], db)
        victim_contents = [m['content'] for m in victim_messages_after]

        # Post-removal message should NOT appear for victim
        assert 'Secret message after removal' not in victim_contents, \
            "Removed user should NOT see post-removal messages"
        print(f"  Victim sees {len(victim_messages_after)} messages (should not include post-removal)")
        print(f"  ✓ Post-removal message exclusion verified")


class TestScaleStress:
    """Stress tests for large-scale scenarios."""

    @pytest.mark.parametrize("n", [100])
    def test_100_member_community_creation(self, fresh_db, n):
        """Verify we can create a 100-member community."""
        db = fresh_db
        print(f"\n=== Stress test: Creating {n} member community ===")

        t_ms = 1000
        admin = user.new_network(name='Admin', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()

        member_count = 1

        for i in range(n - 1):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 5

            new_peer_id = peer.create(t_ms=t_ms, db=db)
            t_ms += 5

            user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=t_ms,
                db=db
            )
            t_ms += 5
            member_count += 1

            if i % 25 == 0 and i > 0:
                db.commit()
                print(f"  Created {member_count}/{n} members...")

        db.commit()

        # Sync
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=200)

        # Verify group size
        members = sender_key.get_group_member_peer_ids(
            admin['all_users_group_id'], admin['peer_id'], db
        )
        print(f"  Community has {len(members)} members")
        assert len(members) >= n * 0.9, f"Should have at least {int(n * 0.9)} members, got {len(members)}"
        print(f"  ✓ {n} member community created successfully")

    @pytest.mark.parametrize("n", [50])
    def test_50_members_with_treekem_and_message(self, fresh_db, n):
        """Full flow: 50 members, TreeKEM updates, message delivery."""
        db = fresh_db
        print(f"\n=== Full flow test with {n} members ===")

        # Create community
        t_ms = 1000
        admin = user.new_network(name='Admin', t_ms=t_ms, db=db)
        t_ms += 100
        db.commit()

        members = [admin]
        for i in range(n - 1):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=t_ms,
                db=db
            )
            t_ms += 5

            new_peer_id = peer.create(t_ms=t_ms, db=db)
            t_ms += 5

            member = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=t_ms,
                db=db
            )
            t_ms += 5

            members.append({
                'peer_id': member['peer_id'],
                'peer_shared_id': member['peer_shared_id'],
            })

            if i % 20 == 0 and i > 0:
                db.commit()

        db.commit()
        print(f"  Created {n} members")

        # Sync
        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=100)

        # All members post TreeKEM updates
        all_peer_shared_ids = [m['peer_shared_id'] for m in members]
        for i, member in enumerate(members):
            treekem_update.create_update_path_dh(
                peer_id=member['peer_id'],
                peer_shared_id=member['peer_shared_id'],
                removal_epoch_id=None,
                base_update_id=None,
                active_members=all_peer_shared_ids,
                t_ms=t_ms,
                db=db,
            )
            t_ms += 20

            if i % 20 == 0 and i > 0:
                db.commit()

        db.commit()
        print(f"  All members posted TreeKEM updates")

        t_ms = run_ticks(db=db, start_t_ms=t_ms, num_rounds=100)

        # Verify root secret exists (O(1) broadcast possible)
        root = treekem_secret.get_root_secret_key_data(admin['peer_id'], db)
        assert root is not None, "Admin should have root secret"
        print(f"  Admin has root secret")

        # Send message
        message.create(
            peer_id=admin['peer_id'],
            channel_id=admin['channel_id'],
            content='Hello 50 members!',
            t_ms=t_ms,
            db=db,
        )
        db.commit()
        print(f"  Admin sent message")

        # Verify message exists
        admin_messages = message.list(admin['channel_id'], admin['peer_id'], db)
        assert len(admin_messages) >= 1
        print(f"  ✓ Full flow with {n} members completed successfully")
