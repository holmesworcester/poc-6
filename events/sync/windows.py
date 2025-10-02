"""Window strategy for bloom-based sync.

Based on ideal protocol design:
- window_id = BLAKE2b-256(event_id) >> (256-w)  (high-order w bits)
- salt = BLAKE2b-128(peer_pk || window_id)
- Dynamic window size: increase w when events > W × 450
- Walk windows in pseudo-random permutation
"""
import hashlib
import struct
from typing import Iterator
from . import constants


def compute_window_id(event_id: bytes, w: int) -> int:
    """Compute window ID for an event.

    Args:
        event_id: 16-byte event ID
        w: Window size parameter (number of bits)

    Returns:
        Window ID (high-order w bits of BLAKE2b-256(event_id))
    """
    # Hash event_id with BLAKE2b-256
    h = hashlib.blake2b(event_id, digest_size=32)
    hash_bytes = h.digest()

    # Convert to integer (big-endian to get high-order bits easily)
    hash_int = int.from_bytes(hash_bytes, byteorder='big')

    # Right shift to get high-order w bits
    shift = 256 - w
    window_id = hash_int >> shift

    return window_id


def derive_salt(peer_pk: bytes, window_id: int) -> bytes:
    """Derive salt for bloom filter from peer public key and window ID.

    Args:
        peer_pk: 32-byte peer public key
        window_id: Window ID integer

    Returns:
        16-byte salt for bloom filter
    """
    # Convert window_id to 4-byte big-endian
    window_id_bytes = window_id.to_bytes(4, byteorder='big')

    # Hash peer_pk || window_id with BLAKE2b-128
    h = hashlib.blake2b(
        peer_pk + window_id_bytes,
        digest_size=16
    )
    return h.digest()


def compute_w_for_event_count(total_events: int) -> int:
    """Compute optimal window parameter w for given event count.

    Args:
        total_events: Total number of events in the system

    Returns:
        Window parameter w (number of bits)
    """
    if total_events == 0:
        return constants.DEFAULT_W

    # Target: ~450 events per window
    # W = 2^w windows needed
    # total_events / W ≈ 450
    # W ≈ total_events / 450
    # w = ceil(log2(total_events / 450))

    import math
    target_windows = max(1, total_events // constants.EVENTS_PER_WINDOW_TARGET)
    w = max(1, math.ceil(math.log2(target_windows)))

    return w


def walk_windows(w: int, last_window: int = -1, peer_pk: bytes = b'') -> Iterator[int]:
    """Generate window IDs to sync in pseudo-random order.

    Args:
        w: Window parameter (number of bits)
        last_window: Last window that was synced (-1 to start from beginning)
        peer_pk: Peer public key for randomization (optional, for future use)

    Yields:
        Window IDs in pseudo-random order, starting after last_window
    """
    total_windows = 2 ** w

    # Simple strategy: walk sequentially with wrap-around
    # Future: Could use a proper pseudo-random permutation based on peer_pk
    start = (last_window + 1) % total_windows

    for i in range(total_windows):
        window_id = (start + i) % total_windows
        yield window_id


def compute_window_count(w: int) -> int:
    """Compute total number of windows for given w parameter.

    Args:
        w: Window parameter (number of bits)

    Returns:
        Total number of windows (2^w)
    """
    return 2 ** w
