"""TreeKEM Scale Tests - full scenario tests at 100-10000 member scale.

Tests the complete TreeKEM flow:
- Network creation and member joins via invites
- TreeKEM update posting and sync
- Key distribution paths (O(1) broadcast, tree cover, leaf fallback)
- Removal handling with old pubkey reuse

Following RULES.md: API queries, deterministic time, assert_eventually for sync.
"""

import pytest
import math
from core import crypto
from core import treekem
from events.identity import user, peer, invite, removal_epoch
from events.group import (
    sender_key,
    secret,
    treekem_update,
    treekem_pubkey_shared,
    treekem_secret,
)
from tests.utils.tick_helper import assert_eventually, run_ticks, TestClock


class TestTreeKEMScale:
    """Full scenario tests for TreeKEM at scale."""

    def create_community(self, n: int, db) -> dict:
        """Create admin + (n-1) members via invite flow.

        Full loopback pattern:
        - Admin creates network
        - Each member joins via peer.create() + user.join(invite)
        - assert_eventually verifies sync convergence

        Uses TestClock to ensure timestamps are consistent with tick_helper's
        _coerce_start_t_ms(), preventing TreeKEMUpdateJob from auto-triggering.

        Uses 10ms step to keep all events within the 10-second JOIN_DELAY_MS window
        (100 members × 3 events = 300 events × 10ms = 3 seconds < 10 seconds).
        """
        clock = TestClock(default_step_ms=10)

        # Admin creates network
        admin = user.new_network(name='Admin', t_ms=clock.tick(), db=db)
        db.commit()

        members = [admin]

        # Create n-1 members via invites
        for i in range(n - 1):
            invite_id, invite_link, _ = invite.create(
                peer_id=admin['peer_id'],
                t_ms=clock.tick(),
                db=db
            )

            new_peer_id = peer.create(t_ms=clock.tick(), db=db)

            member = user.join(
                peer_id=new_peer_id,
                invite_link=invite_link,
                name=f'Member{i}',
                t_ms=clock.tick(),
                db=db
            )

            members.append({
                'peer_id': member['peer_id'],
                'peer_shared_id': member['peer_shared_id'],
                'user_id': member['user_id'],
            })

            # Periodic commits for large N
            if i % 50 == 0:
                db.commit()
                print(f"  Created {i + 1}/{n - 1} members...")

        db.commit()

        # Sync to propagate all events - use clock.now() for consistency
        t_ms = assert_eventually(
            lambda: len(sender_key.get_group_member_peer_ids(
                admin['all_users_group_id'], admin['peer_id'], db
            )) >= n,
            db=db,
            start_t_ms=clock.now(),
            msg=f"All {n} members should be in group"
        )

        return {
            'admin': admin,
            'members': members,
            'group_id': admin['all_users_group_id'],
            't_ms': t_ms,
        }

    def have_all_members_post_updates(self, community: dict, db) -> int:
        """Have every member post a TreeKEM update with copath encryption."""
        t_ms = community['t_ms']
        admin = community['admin']
        all_peer_shared_ids = [m['peer_shared_id'] for m in community['members']]

        for i, member in enumerate(community['members']):
            # Compute copath for this member
            copath = treekem.compute_copath_for_sender(
                member['peer_shared_id'],
                all_peer_shared_ids
            )

            # Get copath pubkeys (may be empty early on)
            copath_pubkey_ids = []
            for depth, path_prefix in copath:
                pk = treekem_pubkey_shared.get_pubkey_at_position(
                    depth, path_prefix,
                    admin['peer_id'],
                    db
                )
                if pk:
                    copath_pubkey_ids.append(crypto.b64encode(pk['id']))

            # Create update path
            treekem_update.create_update_path(
                peer_id=member['peer_id'],
                peer_shared_id=member['peer_shared_id'],
                removal_epoch_id=None,
                base_update_id=None,
                copath_pubkey_ids=copath_pubkey_ids,
                t_ms=t_ms,
                db=db,
            )
            t_ms += 10

            if i % 50 == 0:
                db.commit()
                print(f"  Posted updates for {i + 1}/{len(community['members'])} members...")

        db.commit()

        # Sync until admin has root secret (updates propagated enough for KDF derivation)
        # This is much faster than running a fixed number of rounds
        from events.group import treekem_secret
        t_ms = assert_eventually(
            lambda: treekem_secret.get_root_secret_key_data(admin['peer_id'], db) is not None,
            db=db,
            start_t_ms=t_ms,
            max_rounds=500,
            msg="Admin should derive root secret from updates"
        )
        db.commit()

        return t_ms

    # =========================================================================
    # TEST CASES
    # =========================================================================

    @pytest.mark.parametrize("n", [100])
    def test_scale_o1_broadcast_after_all_updates(self, fresh_db, n):
        """After all members post updates, distribution is O(1)."""
        db = fresh_db
        print(f"\n=== Testing O(1) broadcast with {n} members ===")

        # Setup: create community
        community = self.create_community(n, db)
        print(f"  Community created with {n} members")

        # All members post TreeKEM updates
        t_ms = self.have_all_members_post_updates(community, db)
        print(f"  All members posted updates")

        # Verify we have root secret (needed for O(1) broadcast)
        root_secret = treekem_secret.get_root_secret_key_data(
            community['admin']['peer_id'], db
        )
        assert root_secret is not None, "Admin should have root secret after updates"
        print(f"  Admin has root secret")

        # Distribute a sender key
        secret_id = secret.create(
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            group_id=community['group_id'],
            removal_epoch_id=None,
            t_ms=t_ms,
            db=db,
        )

        result = sender_key.distribute_key_to_group(
            secret_id=secret_id,
            group_id=community['group_id'],
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            removal_epoch_id=None,
            t_ms=t_ms + 1,
            db=db,
        )

        # Verify O(1) broadcast (1 event, not n-1)
        assert len(result) == 1, f"Expected 1 broadcast event, got {len(result)}"
        print(f"  O(1) broadcast verified for {n} members!")

    @pytest.mark.parametrize("n", [100])
    def test_scale_leaf_fallback_before_updates(self, fresh_db, n):
        """Before updates, distribution uses O(n) leaf fallback."""
        db = fresh_db
        print(f"\n=== Testing leaf fallback with {n} members ===")

        # Setup: create community WITHOUT treekem updates
        community = self.create_community(n, db)
        t_ms = community['t_ms']
        print(f"  Community created (no updates posted)")

        # Distribute a sender key
        secret_id = secret.create(
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            group_id=community['group_id'],
            removal_epoch_id=None,
            t_ms=t_ms,
            db=db,
        )

        result = sender_key.distribute_key_to_group(
            secret_id=secret_id,
            group_id=community['group_id'],
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            removal_epoch_id=None,
            t_ms=t_ms + 1,
            db=db,
        )

        # Verify leaf fallback (should be close to n-1 events, one per other member)
        # Note: may be fewer if some members don't have leaf pubkeys yet
        print(f"  Distribution created {len(result)} events")

        # At minimum, should NOT be O(1) broadcast (1 event)
        # It should be O(n) events for leaf fallback
        assert len(result) != 1 or n == 2, \
            f"Expected O(n) events for leaf fallback, got {len(result)}"

        # Should be roughly n-1 (one per recipient)
        expected_min = int((n - 1) * 0.5)  # Allow some tolerance for missing pubkeys
        assert len(result) >= expected_min, \
            f"Expected at least {expected_min} leaf fallback events, got {len(result)}"
        print(f"  Leaf fallback verified: {len(result)} events for {n - 1} recipients")

    @pytest.mark.parametrize("n", [100])
    def test_scale_removal_uses_old_pubkeys(self, fresh_db, n):
        """After removal, distribution uses old pubkeys (no O(n) fallback)."""
        db = fresh_db
        print(f"\n=== Testing removal with {n} members ===")

        # Setup: create community with all updates (O(1) broadcast working)
        community = self.create_community(n, db)
        t_ms = self.have_all_members_post_updates(community, db)

        # Verify O(1) works before removal
        secret_id_1 = secret.create(
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            group_id=community['group_id'],
            removal_epoch_id=None,
            t_ms=t_ms,
            db=db,
        )
        result_before = sender_key.distribute_key_to_group(
            secret_id=secret_id_1,
            group_id=community['group_id'],
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            removal_epoch_id=None,
            t_ms=t_ms + 1,
            db=db,
        )
        assert len(result_before) == 1, "O(1) should work before removal"
        print(f"  O(1) broadcast works before removal")
        t_ms += 10

        # Remove one member (pick one in the middle)
        removed_member = community['members'][n // 2]

        # Get removed member's user_id if they have one
        removed_user_id = removed_member.get('user_id')

        epoch_id = removal_epoch.create(
            removed_peer_id=removed_member['peer_shared_id'],
            removed_user_id=removed_user_id,
            parent_epoch_id=None,
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            t_ms=t_ms + 100,
            db=db,
        )
        db.commit()
        print(f"  Removed member {n // 2}")

        # After removal, the sender needs to post a new update in the new epoch
        # to get a new root secret that excludes the removed member
        # Compute copath for admin
        active_members = sender_key.get_active_members(
            community['group_id'],
            epoch_id,
            community['admin']['peer_id'],
            db
        )
        copath = treekem.compute_copath_for_sender(
            community['admin']['peer_shared_id'],
            active_members
        )

        # Get copath pubkeys
        copath_pubkey_ids = []
        for depth, path_prefix in copath:
            pk = treekem_pubkey_shared.get_pubkey_at_position(
                depth, path_prefix,
                community['admin']['peer_id'],
                db,
                removal_epoch_id=epoch_id
            )
            if pk:
                copath_pubkey_ids.append(crypto.b64encode(pk['id']))

        # Admin posts update in new removal epoch
        treekem_update.create_update_path(
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            removal_epoch_id=epoch_id,
            base_update_id=None,
            copath_pubkey_ids=copath_pubkey_ids,
            t_ms=t_ms + 200,
            db=db,
        )
        db.commit()

        # Sync to propagate the update
        t_ms = run_ticks(db=db, start_t_ms=t_ms + 300, num_rounds=50)
        db.commit()

        # Check if we have root secret for new epoch
        root_secret = treekem_secret.get_root_secret_key_data(
            community['admin']['peer_id'], db
        )

        # Distribute new key with removal epoch
        secret_id_2 = secret.create(
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            group_id=community['group_id'],
            removal_epoch_id=epoch_id,
            t_ms=t_ms + 200,
            db=db,
        )
        result_after = sender_key.distribute_key_to_group(
            secret_id=secret_id_2,
            group_id=community['group_id'],
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            removal_epoch_id=epoch_id,
            t_ms=t_ms + 201,
            db=db,
        )

        # Should NOT be O(n) fallback
        max_expected = int(math.ceil(math.log2(n))) + 5  # Tree cover size + buffer
        print(f"  After removal: {len(result_after)} events (max expected: {max_expected})")

        # If we have root secret, expect O(1)
        # Otherwise expect O(log n) tree cover
        if root_secret:
            assert len(result_after) == 1, \
                f"Expected O(1) broadcast with root secret, got {len(result_after)}"
            print(f"  O(1) broadcast after removal (has root secret)")
        else:
            # Should be tree cover O(log n), not leaf fallback O(n)
            assert len(result_after) <= max_expected, \
                f"Expected O(log n) events, got {len(result_after)}"
            print(f"  O(log n) tree cover after removal ({len(result_after)} events)")

        print(f"  Removal uses old pubkeys correctly!")

    @pytest.mark.parametrize("n", [1000])
    @pytest.mark.slow
    def test_scale_o1_broadcast_1k(self, fresh_db, n):
        """Scale test: O(1) broadcast with 1,000 members.

        This is marked as slow and should be run explicitly:
        pytest -v -s -m slow tests/scenario_tests/test_treekem_scale.py::TestTreeKEMScale::test_scale_o1_broadcast_1k

        Connection limiting (k=20) is enabled by default to avoid O(n²) connection overhead.
        """
        db = fresh_db
        print(f"\n=== Testing O(1) broadcast with {n} members ===")

        # Setup: create community
        community = self.create_community(n, db)
        print(f"  Community created with {n} members")

        # All members post TreeKEM updates
        t_ms = self.have_all_members_post_updates(community, db)
        print(f"  All members posted updates")

        # Verify we have root secret
        root_secret = treekem_secret.get_root_secret_key_data(
            community['admin']['peer_id'], db
        )
        assert root_secret is not None, "Admin should have root secret"

        # Distribute a sender key
        secret_id = secret.create(
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            group_id=community['group_id'],
            removal_epoch_id=None,
            t_ms=t_ms,
            db=db,
        )

        result = sender_key.distribute_key_to_group(
            secret_id=secret_id,
            group_id=community['group_id'],
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            removal_epoch_id=None,
            t_ms=t_ms + 1,
            db=db,
        )

        # Verify O(1) broadcast
        assert len(result) == 1, f"Expected 1 broadcast event, got {len(result)}"
        print(f"  O(1) broadcast verified for {n} members!")

    @pytest.mark.parametrize("n", [10000])
    @pytest.mark.slow
    def test_scale_o1_broadcast_10k(self, fresh_db, n):
        """Stress test: O(1) broadcast with 10,000 members.

        This is marked as slow and should be run explicitly:
        pytest -v -s -m slow tests/scenario_tests/test_treekem_scale.py::TestTreeKEMScale::test_scale_o1_broadcast_10k
        """
        db = fresh_db
        print(f"\n=== Testing O(1) broadcast with {n} members (STRESS TEST) ===")

        # Setup: create community
        community = self.create_community(n, db)
        print(f"  Community created with {n} members")

        # All members post TreeKEM updates
        t_ms = self.have_all_members_post_updates(community, db)
        print(f"  All members posted updates")

        # Verify we have root secret
        root_secret = treekem_secret.get_root_secret_key_data(
            community['admin']['peer_id'], db
        )
        assert root_secret is not None, "Admin should have root secret"

        # Distribute a sender key
        secret_id = secret.create(
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            group_id=community['group_id'],
            removal_epoch_id=None,
            t_ms=t_ms,
            db=db,
        )

        result = sender_key.distribute_key_to_group(
            secret_id=secret_id,
            group_id=community['group_id'],
            peer_id=community['admin']['peer_id'],
            peer_shared_id=community['admin']['peer_shared_id'],
            removal_epoch_id=None,
            t_ms=t_ms + 1,
            db=db,
        )

        # Verify O(1) broadcast
        assert len(result) == 1, f"Expected 1 broadcast event, got {len(result)}"
        print(f"  O(1) broadcast verified for {n} members!")


class TestTreeKEMScaleMetrics:
    """Tests that measure and report TreeKEM scaling characteristics."""

    @pytest.mark.parametrize("n", [10, 50, 100, 500])
    def test_copath_size_scales_logarithmically(self, n):
        """Verify copath size is O(log n)."""
        # Create synthetic peer IDs
        peer_ids = [
            crypto.b64encode(crypto.generate_secret()[:16])
            for _ in range(n)
        ]

        # Pick a random sender
        sender = peer_ids[0]

        # Compute copath
        copath = treekem.compute_copath_for_sender(sender, peer_ids)

        # Expected: O(log n) nodes
        expected_max = int(math.ceil(math.log2(n))) + 2  # +2 for edge cases
        expected_min = max(1, int(math.floor(math.log2(n))) - 1)

        print(f"  n={n}: copath size={len(copath)}, expected range=[{expected_min}, {expected_max}]")

        assert len(copath) <= expected_max, \
            f"Copath too large: {len(copath)} > {expected_max}"
        assert len(copath) >= expected_min, \
            f"Copath too small: {len(copath)} < {expected_min}"

    def test_tree_cover_for_sender_at_scale(self):
        """Verify sender's tree cover (copath) scales as O(log n)."""
        test_sizes = [100, 500, 1000]

        for n in test_sizes:
            # Create synthetic peer IDs
            peer_ids = [
                crypto.b64encode(crypto.generate_secret()[:16])
                for _ in range(n)
            ]

            # Sender's copath to reach all other peers is O(log n)
            sender = peer_ids[0]
            copath = treekem.compute_copath_for_sender(sender, peer_ids)

            # Copath should be O(log n) in size
            expected_max = int(math.ceil(math.log2(n))) + 3

            print(f"  n={n}: copath size={len(copath)}, expected max={expected_max}")

            assert len(copath) <= expected_max, \
                f"Copath too large at n={n}: {len(copath)} > {expected_max}"
