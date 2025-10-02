"""Bloom filter implementation for sync protocol.

Based on ideal protocol design:
- 512 bits (64 bytes) bloom filter
- k=5 hash functions
- Bloom semantics: Tells responder what requester does NOT have
- False positives result in events NOT being sent
"""
import hashlib
from typing import List
from . import constants


def create_bloom(event_ids: List[bytes], salt: bytes) -> bytes:
    """Create a bloom filter from a list of event IDs.

    Args:
        event_ids: List of event IDs (16 bytes each) that requester HAS
        salt: 16-byte salt for this window (derived from peer_pk || window_id)

    Returns:
        64-byte bloom filter representing events requester has
    """
    # Initialize empty bloom filter (all zeros)
    bloom = bytearray(constants.BLOOM_SIZE_BYTES)

    for event_id in event_ids:
        # Set k=5 bits for this event_id
        for k in range(constants.K_HASHES):
            # Hash with salt and hash index
            bit_index = _hash_to_bit_index(event_id, salt, k)
            # Set the bit
            byte_index = bit_index // 8
            bit_offset = bit_index % 8
            bloom[byte_index] |= (1 << bit_offset)

    return bytes(bloom)


def check_bloom(event_id: bytes, bloom: bytes, salt: bytes) -> bool:
    """Check if an event ID is in the bloom filter.

    Args:
        event_id: 16-byte event ID to check
        bloom: 64-byte bloom filter
        salt: 16-byte salt used to create the bloom

    Returns:
        True if event_id is PROBABLY in the bloom (requester has it)
        False if event_id is DEFINITELY NOT in the bloom (requester doesn't have it)
    """
    for k in range(constants.K_HASHES):
        bit_index = _hash_to_bit_index(event_id, salt, k)
        byte_index = bit_index // 8
        bit_offset = bit_index % 8

        # Check if bit is set
        if not (bloom[byte_index] & (1 << bit_offset)):
            # At least one bit is not set -> definitely not in bloom
            return False

    # All k bits are set -> probably in bloom (could be false positive)
    return True


def _hash_to_bit_index(event_id: bytes, salt: bytes, k: int) -> int:
    """Hash event_id with salt and k index to get a bit index in [0, 512).

    Uses BLAKE2b with personalization parameter for k.
    """
    # Use BLAKE2b with personalization for k
    h = hashlib.blake2b(
        event_id + salt,
        digest_size=8,  # 64 bits is enough for our 512-bit space
        person=f"bloom-k{k}".encode()[:16]  # Personalization max 16 bytes
    )
    # Convert to integer and modulo to get bit index
    hash_val = int.from_bytes(h.digest(), byteorder='little')
    return hash_val % constants.BLOOM_SIZE_BITS
