"""TreeKEM tree utilities for O(log n) key distribution on removal.

TreeKEM uses a binary tree where each group member occupies a leaf position.
The position is derived from the member's peer_shared_id bits.

Tree structure:
- Depth 0: Root node (covers entire group)
- Depth 1: Left/right subtrees (bit 0 of peer_shared_id)
- Depth 2: Quarters (bit 1)
- etc.

Path prefix:
- Empty bytes = root
- 0x00 = left child at depth 1
- 0x01 = right child at depth 1
- 0x00 0x00 = left-left at depth 2
- etc.

Each node has:
- depth: Distance from root (0 = root)
- path_prefix: Sequence of bits indicating position
"""

from typing import Any
import logging
from core import crypto

log = logging.getLogger(__name__)

# Maximum tree depth (16 levels = 65536 leaf positions)
MAX_DEPTH = 16

# TreeKEM pubkey TTL (1 hour in milliseconds)
TREEKEM_PUBKEY_TTL_MS = 60 * 60 * 1000

# TreeKEM update job cadence (5 minutes in milliseconds)
TREEKEM_UPDATE_INTERVAL_MS = 5 * 60 * 1000

# Refresh threshold: refresh pubkey when TTL < this value
TREEKEM_REFRESH_THRESHOLD_MS = 15 * 60 * 1000


def leaf_position_from_peer_id(peer_shared_id: str) -> int:
    """Compute leaf position from peer_shared_id.

    Uses the first 16 bits of the peer_shared_id hash to determine position.
    This gives us a pseudo-random but deterministic leaf position.

    Args:
        peer_shared_id: Base64-encoded peer_shared_id

    Returns:
        Leaf position integer (0 to 65535)
    """
    id_bytes = crypto.b64decode(peer_shared_id)
    # Use first 2 bytes to get position (0-65535)
    return int.from_bytes(id_bytes[:2], 'little')


def path_prefix_for_leaf(position: int, depth: int) -> bytes:
    """Get the path prefix to reach a given depth for a leaf position.

    Args:
        position: Leaf position (0-65535)
        depth: Target depth (0 = root, MAX_DEPTH = leaf)

    Returns:
        Path prefix bytes (one byte per depth level, each 0 or 1)
    """
    if depth == 0:
        return b''
    if depth > MAX_DEPTH:
        depth = MAX_DEPTH

    prefix = bytearray(depth)
    for d in range(depth):
        # Extract bit d from position
        bit = (position >> d) & 1
        prefix[d] = bit
    return bytes(prefix)


def path_to_root(peer_shared_id: str) -> list[tuple[int, bytes]]:
    """Get the path from a peer's leaf to the root.

    Returns a list of (depth, path_prefix) tuples from leaf to root.
    Excludes the leaf itself (caller handles that separately).

    Args:
        peer_shared_id: Base64-encoded peer_shared_id

    Returns:
        List of (depth, path_prefix) from depth MAX_DEPTH-1 down to depth 0 (root)
    """
    position = leaf_position_from_peer_id(peer_shared_id)
    path = []

    # Start from parent of leaf (MAX_DEPTH - 1) down to root (0)
    for depth in range(MAX_DEPTH - 1, -1, -1):
        prefix = path_prefix_for_leaf(position, depth)
        path.append((depth, prefix))

    return path


def copath_for_removal(removed_peer_shared_id: str) -> list[tuple[int, bytes]]:
    """Get the copath for a removed member.

    The copath consists of siblings of nodes on the path from the removed member's
    leaf to the root. Encrypting to copath nodes allows efficient key distribution:
    all members NOT in the removed member's subtree can decrypt.

    Args:
        removed_peer_shared_id: Base64-encoded peer_shared_id of removed member

    Returns:
        List of (depth, path_prefix) for sibling nodes from leaf to root
    """
    position = leaf_position_from_peer_id(removed_peer_shared_id)
    copath = []

    # For each level, compute the sibling of the node on the removed member's path
    for depth in range(1, MAX_DEPTH + 1):
        # Get the path prefix for this depth
        prefix = path_prefix_for_leaf(position, depth)

        # The sibling has the same prefix but with the last bit flipped
        if len(prefix) > 0:
            sibling_prefix = bytearray(prefix)
            sibling_prefix[-1] = 1 - sibling_prefix[-1]  # Flip last bit
            copath.append((depth, bytes(sibling_prefix)))

    return copath


def is_covered_by_node(peer_shared_id: str, node_depth: int, node_prefix: bytes) -> bool:
    """Check if a peer is covered by a tree node.

    A peer is covered by a node if the peer's path prefix at that depth
    matches the node's prefix.

    Args:
        peer_shared_id: Base64-encoded peer_shared_id
        node_depth: Depth of the node
        node_prefix: Path prefix of the node

    Returns:
        True if the peer is in the node's subtree
    """
    if node_depth == 0:
        return True  # Root covers everyone

    position = leaf_position_from_peer_id(peer_shared_id)
    peer_prefix = path_prefix_for_leaf(position, node_depth)

    return peer_prefix == node_prefix


def get_covering_nodes_for_removal(
    removed_peer_shared_id: str,
    members: list[str],
    valid_nodes: set[tuple[int, bytes]]
) -> tuple[list[tuple[int, bytes]], set[str]]:
    """Find the optimal set of tree nodes to cover all remaining members.

    Uses a greedy algorithm to find nodes that:
    1. Are in the copath of the removed member (don't cover the removed member)
    2. Have valid pubkeys (in valid_nodes set)
    3. Together cover all remaining members with minimum redundancy

    Args:
        removed_peer_shared_id: The removed member's peer_shared_id
        members: List of peer_shared_ids of remaining members
        valid_nodes: Set of (depth, path_prefix) with valid pubkeys

    Returns:
        Tuple of (selected_nodes, covered_members) where:
        - selected_nodes: List of (depth, path_prefix) to encrypt to
        - covered_members: Set of peer_shared_ids covered by TreeKEM
    """
    copath = copath_for_removal(removed_peer_shared_id)
    covered: set[str] = set()
    selected: list[tuple[int, bytes]] = []

    # Sort copath by depth (lower depth = larger subtree = more members covered)
    # This greedy approach prioritizes larger subtrees first
    for depth, prefix in sorted(copath, key=lambda x: x[0]):
        node_key = (depth, prefix)

        # Skip if this node doesn't have a valid pubkey
        if node_key not in valid_nodes:
            continue

        # Find members covered by this node that aren't already covered
        newly_covered = set()
        for member in members:
            if member not in covered and is_covered_by_node(member, depth, prefix):
                newly_covered.add(member)

        # Only use this node if it covers new members
        if newly_covered:
            selected.append(node_key)
            covered.update(newly_covered)

            # Early exit if all members are covered
            if len(covered) == len(members):
                break

    return selected, covered


def node_id_from_position(depth: int, path_prefix: bytes) -> str:
    """Create a unique node identifier string.

    Args:
        depth: Node depth
        path_prefix: Node path prefix

    Returns:
        String like "d3:001" (depth 3, path prefix 0-0-1)
    """
    prefix_str = ''.join(str(b) for b in path_prefix) if path_prefix else ''
    return f"d{depth}:{prefix_str}"


def parse_node_id(node_id: str) -> tuple[int, bytes]:
    """Parse a node identifier string.

    Args:
        node_id: String like "d3:001"

    Returns:
        Tuple of (depth, path_prefix)
    """
    parts = node_id.split(':')
    depth = int(parts[0][1:])  # Remove 'd' prefix
    prefix_str = parts[1] if len(parts) > 1 else ''
    path_prefix = bytes(int(c) for c in prefix_str) if prefix_str else b''
    return depth, path_prefix
