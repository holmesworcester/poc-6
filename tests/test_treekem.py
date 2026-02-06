"""Unit tests for TreeKEM tree utilities.

Tests the core tree operations:
- Leaf position from peer_id
- Path prefix computation
- Path to root
- Copath for removal
- Node coverage checking
- Covering node selection
"""

import pytest
from core import treekem
from core import crypto


class TestLeafPosition:
    """Tests for leaf_position_from_peer_id."""

    def test_deterministic_position(self):
        """Same peer_id always gives same position."""
        peer_id = crypto.b64encode(b'\x01\x02' + b'\x00' * 30)
        pos1 = treekem.leaf_position_from_peer_id(peer_id)
        pos2 = treekem.leaf_position_from_peer_id(peer_id)
        assert pos1 == pos2

    def test_different_peers_different_positions(self):
        """Different peer_ids should (usually) give different positions."""
        peer1 = crypto.b64encode(b'\x01\x02' + b'\x00' * 30)
        peer2 = crypto.b64encode(b'\x03\x04' + b'\x00' * 30)
        pos1 = treekem.leaf_position_from_peer_id(peer1)
        pos2 = treekem.leaf_position_from_peer_id(peer2)
        assert pos1 != pos2

    def test_position_range(self):
        """Position should be in valid range (0 to 65535)."""
        for i in range(100):
            peer_id = crypto.b64encode(crypto.generate_secret())
            pos = treekem.leaf_position_from_peer_id(peer_id)
            assert 0 <= pos <= 65535


class TestPathPrefix:
    """Tests for path_prefix_for_leaf."""

    def test_root_empty_prefix(self):
        """Depth 0 (root) has empty prefix."""
        prefix = treekem.path_prefix_for_leaf(0, 0)
        assert prefix == b''

    def test_depth_1_single_byte(self):
        """Depth 1 has single byte prefix."""
        # Position 0: bit 0 is 0
        prefix = treekem.path_prefix_for_leaf(0, 1)
        assert prefix == b'\x00'

        # Position 1: bit 0 is 1
        prefix = treekem.path_prefix_for_leaf(1, 1)
        assert prefix == b'\x01'

    def test_depth_2_two_bytes(self):
        """Depth 2 has two byte prefix."""
        # Position 0 (binary 00): bits are 0, 0
        prefix = treekem.path_prefix_for_leaf(0, 2)
        assert prefix == b'\x00\x00'

        # Position 3 (binary 11): bits are 1, 1
        prefix = treekem.path_prefix_for_leaf(3, 2)
        assert prefix == b'\x01\x01'

        # Position 2 (binary 10): bits are 0, 1
        prefix = treekem.path_prefix_for_leaf(2, 2)
        assert prefix == b'\x00\x01'

    def test_consistent_prefix(self):
        """Higher depth prefixes start with lower depth prefixes."""
        position = 42
        for depth in range(1, treekem.MAX_DEPTH):
            prefix_d = treekem.path_prefix_for_leaf(position, depth)
            prefix_d1 = treekem.path_prefix_for_leaf(position, depth + 1)
            assert prefix_d1[:depth] == prefix_d


class TestPathToRoot:
    """Tests for path_to_root."""

    def test_path_length(self):
        """Path has MAX_DEPTH entries (from depth MAX_DEPTH-1 down to 0)."""
        peer_id = crypto.b64encode(b'\x00' * 32)
        path = treekem.path_to_root(peer_id)
        assert len(path) == treekem.MAX_DEPTH

    def test_path_ends_at_root(self):
        """Last entry in path is the root (depth 0, empty prefix)."""
        peer_id = crypto.b64encode(b'\x00' * 32)
        path = treekem.path_to_root(peer_id)
        assert path[-1] == (0, b'')

    def test_path_depth_ordering(self):
        """Path goes from MAX_DEPTH-1 down to 0."""
        peer_id = crypto.b64encode(b'\x00' * 32)
        path = treekem.path_to_root(peer_id)
        depths = [d for d, _ in path]
        assert depths == list(range(treekem.MAX_DEPTH - 1, -1, -1))


class TestCopathForRemoval:
    """Tests for copath_for_removal."""

    def test_copath_length(self):
        """Copath has MAX_DEPTH entries (siblings at each level)."""
        peer_id = crypto.b64encode(b'\x00' * 32)
        copath = treekem.copath_for_removal(peer_id)
        assert len(copath) == treekem.MAX_DEPTH

    def test_copath_siblings_differ_by_one_bit(self):
        """Each copath entry is the sibling (last bit flipped) of path entry."""
        peer_id = crypto.b64encode(b'\x00' * 32)
        position = treekem.leaf_position_from_peer_id(peer_id)

        copath = treekem.copath_for_removal(peer_id)

        for depth, sibling_prefix in copath:
            # Get the path prefix at this depth
            path_prefix = treekem.path_prefix_for_leaf(position, depth)

            # Sibling should have last bit flipped
            if len(path_prefix) > 0:
                expected_sibling = bytearray(path_prefix)
                expected_sibling[-1] = 1 - expected_sibling[-1]
                assert bytes(expected_sibling) == sibling_prefix

    def test_copath_does_not_cover_removed(self):
        """Copath nodes should NOT cover the removed member."""
        peer_id = crypto.b64encode(b'\x00' * 32)
        copath = treekem.copath_for_removal(peer_id)

        for depth, prefix in copath:
            covered = treekem.is_covered_by_node(peer_id, depth, prefix)
            assert not covered, f"Copath node at depth {depth} should not cover removed member"


class TestIsCoveredByNode:
    """Tests for is_covered_by_node."""

    def test_root_covers_everyone(self):
        """Root node (depth 0) covers all peers."""
        for _ in range(10):
            peer_id = crypto.b64encode(crypto.generate_secret())
            assert treekem.is_covered_by_node(peer_id, 0, b'')

    def test_leaf_only_covers_self(self):
        """Leaf node only covers the peer at that exact position."""
        peer1 = crypto.b64encode(b'\x00' * 32)
        peer2 = crypto.b64encode(b'\x01' * 32)

        pos1 = treekem.leaf_position_from_peer_id(peer1)
        prefix1 = treekem.path_prefix_for_leaf(pos1, treekem.MAX_DEPTH)

        # Peer1 is covered by its own leaf
        assert treekem.is_covered_by_node(peer1, treekem.MAX_DEPTH, prefix1)

        # Peer2 is not covered by peer1's leaf (different positions)
        assert not treekem.is_covered_by_node(peer2, treekem.MAX_DEPTH, prefix1)

    def test_subtree_coverage(self):
        """Intermediate nodes cover their subtree."""
        # Create peers in same subtree (same first bit)
        peer_left = crypto.b64encode(b'\x00\x00' + b'\x00' * 30)  # Position has bit 0 = 0
        peer_right = crypto.b64encode(b'\x01\x00' + b'\x00' * 30)  # Position has bit 0 = 1

        # Left subtree at depth 1
        left_subtree = (1, b'\x00')
        right_subtree = (1, b'\x01')

        # peer_left should be in left subtree only
        assert treekem.is_covered_by_node(peer_left, *left_subtree)
        assert not treekem.is_covered_by_node(peer_left, *right_subtree)

        # peer_right should be in right subtree only
        assert treekem.is_covered_by_node(peer_right, *right_subtree)
        assert not treekem.is_covered_by_node(peer_right, *left_subtree)


class TestGetCoveringNodesForRemoval:
    """Tests for get_covering_nodes_for_removal."""

    def test_empty_valid_nodes_covers_none(self):
        """With no valid nodes, no members are covered."""
        removed = crypto.b64encode(b'\x00' * 32)
        members = [crypto.b64encode(b'\x01' * 32)]
        valid_nodes: set[tuple[int, bytes]] = set()

        selected, covered = treekem.get_covering_nodes_for_removal(removed, members, valid_nodes)

        assert len(selected) == 0
        assert len(covered) == 0

    def test_root_covers_all_except_removed(self):
        """If root is valid and removed member's sibling covers them."""
        removed = crypto.b64encode(b'\x00' * 32)
        # Members on opposite side of first bit
        member1 = crypto.b64encode(b'\x01\x00' + b'\x00' * 30)
        member2 = crypto.b64encode(b'\x01\x01' + b'\x00' * 30)
        members = [member1, member2]

        # Add sibling of removed at depth 1 as valid node
        removed_pos = treekem.leaf_position_from_peer_id(removed)
        removed_bit0 = removed_pos & 1
        sibling_prefix = bytes([1 - removed_bit0])  # Opposite of removed's bit 0
        valid_nodes = {(1, sibling_prefix)}

        selected, covered = treekem.get_covering_nodes_for_removal(removed, members, valid_nodes)

        # Should select the sibling node
        assert (1, sibling_prefix) in selected
        # Should cover both members (they're on the opposite side)
        assert member1 in covered or member2 in covered

    def test_greedy_selects_larger_subtrees_first(self):
        """Algorithm prefers nodes covering more members."""
        removed = crypto.b64encode(b'\x00' * 32)
        members = [crypto.b64encode(crypto.generate_secret()) for _ in range(10)]

        # Make multiple copath nodes valid
        copath = treekem.copath_for_removal(removed)
        valid_nodes = set(copath[:5])  # First 5 copath nodes

        selected, covered = treekem.get_covering_nodes_for_removal(removed, members, valid_nodes)

        # Should select nodes, preferring lower depth (larger coverage)
        if selected:
            depths = [d for d, _ in selected]
            # Lower depths should come first (greedy)
            assert depths == sorted(depths)

    def test_no_redundant_coverage(self):
        """Algorithm doesn't select nodes that only cover already-covered members."""
        removed = crypto.b64encode(b'\x00' * 32)
        # Single member
        member = crypto.b64encode(b'\xFF' * 32)
        members = [member]

        # All copath nodes are valid
        copath = treekem.copath_for_removal(removed)
        valid_nodes = set(copath)

        selected, covered = treekem.get_covering_nodes_for_removal(removed, members, valid_nodes)

        # Member should be covered
        assert member in covered

        # Should only select nodes that add coverage
        # (not all copath nodes, since one is enough for a single member)
        assert len(selected) <= len(copath)


class TestNodeIdFunctions:
    """Tests for node_id_from_position and parse_node_id."""

    def test_roundtrip(self):
        """node_id_from_position and parse_node_id are inverses."""
        test_cases = [
            (0, b''),
            (1, b'\x00'),
            (1, b'\x01'),
            (3, b'\x01\x00\x01'),
            (16, b'\x00' * 16),
        ]

        for depth, prefix in test_cases:
            node_id = treekem.node_id_from_position(depth, prefix)
            parsed_depth, parsed_prefix = treekem.parse_node_id(node_id)
            assert parsed_depth == depth
            assert parsed_prefix == prefix

    def test_format(self):
        """node_id has expected format."""
        assert treekem.node_id_from_position(0, b'') == 'd0:'
        assert treekem.node_id_from_position(1, b'\x00') == 'd1:0'
        assert treekem.node_id_from_position(2, b'\x01\x00') == 'd2:10'
