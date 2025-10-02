"""Constants for bloom-based sync protocol.

Based on ideal protocol design from poc-5.
"""

# Bloom filter parameters
BLOOM_SIZE_BITS = 512  # 512 bits = 64 bytes
BLOOM_SIZE_BYTES = 64
K_HASHES = 5  # Number of hash functions

# Window parameters
DEFAULT_W = 12  # Default window parameter: 2^12 = 4096 windows
STORAGE_W = 20  # Storage window parameter: 2^20 = 1M windows (future-proof)
EVENTS_PER_WINDOW_TARGET = 450  # Target events per window for optimal FPR

# False Positive Rate target (informational, not used in code)
# For 76 events/window with 512-bit bloom and k=5:
# FPR ≈ 2.5% (wasted bandwidth from false positives)
TARGET_FPR = 0.025
