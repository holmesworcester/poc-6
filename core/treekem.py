"""TreeKEM tree utilities for O(log n) key distribution.

The tree uses peer_shared_id bits to derive positions:
- Each bit determines left(0)/right(1) at that depth
- Path prefix = first `depth` bits of peer_shared_id
- Copath = sibling nodes from leaf to root

O(log n) achieved by encrypting to copath nodes instead of every peer.
"""

from core import crypto

# Practical limit for tree depth (2^20 = ~1M peers)
MAX_TREE_DEPTH = 20


def get_path_prefix(peer_shared_id: str, depth: int) -> bytes:
    """Get the path prefix for a peer at a given depth.

    The path prefix is the first `depth` bits of the peer_shared_id,
    packed into bytes (ceil(depth/8) bytes).

    Args:
        peer_shared_id: Base64-encoded peer shared ID (16 bytes decoded)
        depth: Tree depth (0 = root, higher = more specific)

    Returns:
        Path prefix bytes (up to 16 bytes)
    """
    if depth < 0 or depth > MAX_TREE_DEPTH:
        raise ValueError(f"depth must be 0-{MAX_TREE_DEPTH}, got {depth}")

    if depth == 0:
        return b''

    peer_bytes = crypto.b64decode(peer_shared_id)
    if len(peer_bytes) != 16:
        raise ValueError(f"peer_shared_id must be 16 bytes, got {len(peer_bytes)}")

    # Extract first `depth` bits
    # Each byte has 8 bits, so we need ceil(depth/8) bytes
    num_bytes = (depth + 7) // 8
    prefix = bytearray(peer_bytes[:num_bytes])

    # Mask off extra bits in the last byte
    bits_in_last_byte = depth % 8
    if bits_in_last_byte != 0:
        # Keep only the first `bits_in_last_byte` bits
        mask = (0xFF << (8 - bits_in_last_byte)) & 0xFF
        prefix[-1] &= mask

    return bytes(prefix)


def get_copath_prefix(peer_shared_id: str, depth: int) -> bytes:
    """Get the copath (sibling) prefix for a peer at a given depth.

    The copath prefix is the path prefix with the bit at position `depth-1` flipped.
    This represents the sibling subtree at that level.

    Args:
        peer_shared_id: Base64-encoded peer shared ID
        depth: Tree depth (must be >= 1)

    Returns:
        Copath prefix bytes
    """
    if depth < 1:
        raise ValueError("depth must be >= 1 for copath")

    path = get_path_prefix(peer_shared_id, depth)
    if not path:
        return path

    # Flip the last bit (bit at position depth-1)
    prefix = bytearray(path)
    bit_index = (depth - 1) % 8
    byte_index = (depth - 1) // 8

    # Flip the bit (from left side of byte)
    flip_mask = 0x80 >> bit_index
    prefix[byte_index] ^= flip_mask

    return bytes(prefix)


def path_covers_peer(path_prefix: bytes, depth: int, peer_shared_id: str) -> bool:
    """Check if a path prefix covers a peer.

    A path covers a peer if the peer's path prefix at the given depth
    matches the given path prefix.

    Args:
        path_prefix: The path prefix to check
        depth: The depth of the path
        peer_shared_id: The peer to check

    Returns:
        True if the path covers the peer
    """
    if depth == 0:
        return True  # Root covers everyone

    peer_path = get_path_prefix(peer_shared_id, depth)
    return peer_path == path_prefix


def get_bit_at_depth(peer_shared_id: str, depth: int) -> int:
    """Get the bit value (0 or 1) at a specific depth.

    Args:
        peer_shared_id: Base64-encoded peer shared ID
        depth: Tree depth (0-indexed from root)

    Returns:
        0 or 1 indicating left or right branch
    """
    if depth < 0:
        raise ValueError("depth must be >= 0")

    peer_bytes = crypto.b64decode(peer_shared_id)
    byte_index = depth // 8
    bit_index = depth % 8

    if byte_index >= len(peer_bytes):
        return 0

    # Bit index 0 is the MSB
    return (peer_bytes[byte_index] >> (7 - bit_index)) & 1


def compute_tree_cover(
    peer_shared_ids: list[str],
    exclude_peer_id: str | None = None,
    max_depth: int = MAX_TREE_DEPTH
) -> list[tuple[int, bytes]]:
    """Compute the minimal set of tree nodes that cover all peers.

    This is used to compute the copath - the minimal set of subtree roots
    that, together with the sender's path, cover all recipients.

    The algorithm:
    1. Build a trie of all peer paths
    2. Find the minimal covering set by collapsing full subtrees

    Args:
        peer_shared_ids: List of peer shared IDs to cover
        exclude_peer_id: Optional peer to exclude (typically the sender)
        max_depth: Maximum tree depth to consider

    Returns:
        List of (depth, path_prefix) tuples representing covering nodes
    """
    if not peer_shared_ids:
        return []

    # Filter out excluded peer
    peers = [p for p in peer_shared_ids if p != exclude_peer_id]
    if not peers:
        return []

    # Build a set of leaf paths for efficient lookup
    leaf_paths: set[bytes] = set()
    for peer_id in peers:
        # Use full 128-bit path as leaf identifier
        peer_bytes = crypto.b64decode(peer_id)
        leaf_paths.add(peer_bytes)

    # Start from root and recursively find covering nodes
    covers: list[tuple[int, bytes]] = []
    _find_covers(b'', 0, leaf_paths, max_depth, covers)

    return covers


def _find_covers(
    prefix: bytes,
    depth: int,
    leaf_paths: set[bytes],
    max_depth: int,
    covers: list[tuple[int, bytes]]
) -> bool:
    """Recursively find covering nodes.

    Returns True if this subtree has any leaves (is non-empty).
    """
    # Count leaves in this subtree
    leaves_in_subtree = [
        lp for lp in leaf_paths
        if _prefix_matches(prefix, depth, lp)
    ]

    if not leaves_in_subtree:
        return False  # Empty subtree

    # If we're at max depth or only have one distinct path, emit this node
    if depth >= max_depth or len(leaves_in_subtree) == 1:
        covers.append((depth, prefix))
        return True

    # Check if ALL possible leaves at this depth are covered
    # If so, we can emit just this node instead of children
    # For simplicity, we always recurse and let children collapse

    # Recurse into left and right children
    left_prefix = _extend_prefix(prefix, depth, 0)
    right_prefix = _extend_prefix(prefix, depth, 1)

    left_has = _find_covers(left_prefix, depth + 1, leaf_paths, max_depth, covers)
    right_has = _find_covers(right_prefix, depth + 1, leaf_paths, max_depth, covers)

    return left_has or right_has


def _prefix_matches(prefix: bytes, depth: int, full_path: bytes) -> bool:
    """Check if a full path matches a prefix at a given depth."""
    if depth == 0:
        return True

    num_bytes = (depth + 7) // 8
    if len(prefix) < num_bytes:
        return False

    # Check full bytes
    full_bytes = (depth) // 8
    if full_path[:full_bytes] != prefix[:full_bytes]:
        return False

    # Check partial byte if any
    bits_in_last = depth % 8
    if bits_in_last > 0 and full_bytes < len(prefix):
        mask = (0xFF << (8 - bits_in_last)) & 0xFF
        if (full_path[full_bytes] & mask) != (prefix[full_bytes] & mask):
            return False

    return True


def _extend_prefix(prefix: bytes, depth: int, bit: int) -> bytes:
    """Extend a prefix by adding a bit at the given depth."""
    new_depth = depth + 1
    num_bytes = (new_depth + 7) // 8

    # Start with existing prefix padded to needed length
    result = bytearray(num_bytes)
    result[:len(prefix)] = prefix

    # Set the bit at position `depth`
    byte_index = depth // 8
    bit_index = depth % 8

    if bit:
        result[byte_index] |= (0x80 >> bit_index)
    else:
        result[byte_index] &= ~(0x80 >> bit_index) & 0xFF

    return bytes(result)


def compute_copath_for_sender(
    sender_peer_id: str,
    all_peer_ids: list[str],
    max_depth: int = MAX_TREE_DEPTH
) -> list[tuple[int, bytes]]:
    """Compute the copath nodes for a sender to reach all other peers.

    The copath consists of sibling nodes along the sender's path to the root.
    Each copath node covers all peers in its subtree that are NOT in the
    sender's subtree.

    Args:
        sender_peer_id: The sending peer's shared ID
        all_peer_ids: All peers in the group (including sender)
        max_depth: Maximum depth to consider

    Returns:
        List of (depth, path_prefix) tuples for copath nodes that have recipients
    """
    other_peers = [p for p in all_peer_ids if p != sender_peer_id]
    if not other_peers:
        return []

    copath: list[tuple[int, bytes]] = []

    # Walk up from the deepest needed level to the root
    # At each level, check if the sibling subtree has any recipients
    for depth in range(1, max_depth + 1):
        sibling_prefix = get_copath_prefix(sender_peer_id, depth)

        # Check if any other peer is covered by this sibling subtree
        has_recipients = any(
            path_covers_peer(sibling_prefix, depth, peer_id)
            for peer_id in other_peers
        )

        if has_recipients:
            copath.append((depth, sibling_prefix))

        # If all other peers are now covered, we can stop
        covered = set()
        for d, prefix in copath:
            for peer_id in other_peers:
                if path_covers_peer(prefix, d, peer_id):
                    covered.add(peer_id)

        if covered == set(other_peers):
            break

    return copath


def copath_size(num_peers: int) -> int:
    """Estimate the copath size for a group of n peers.

    In a balanced binary tree with n leaves, the copath from any leaf
    to the root has O(log n) nodes. In practice with random peer IDs,
    the tree is approximately balanced.

    Args:
        num_peers: Number of peers in the group

    Returns:
        Estimated copath size (O(log n))
    """
    if num_peers <= 1:
        return 0

    # ceil(log2(num_peers))
    import math
    return int(math.ceil(math.log2(num_peers)))
