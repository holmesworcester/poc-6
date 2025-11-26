"""SyncWindow: Encapsulates sync window logic and computations."""
from typing import Iterator
import hashlib
import math
import logging

log = logging.getLogger(__name__)

# Window parameters
DEFAULT_W = 12  # Default window parameter: 2^12 = 4096 windows
STORAGE_W = 20  # Storage window parameter: 2^20 = 1M windows
EVENTS_PER_WINDOW_TARGET = 450  # Target events per window for optimal FPR


class SyncWindow:
    """Encapsulate all window-related logic and computations.

    This class handles:
    - Computing window IDs from event IDs
    - Mapping between query windows and storage windows
    - Computing optimal window parameter (w) for event counts
    - Generating window traversal order

    All window logic is centralized here instead of scattered across sync.py.
    """

    def __init__(self, w: int, query_window_id: int):
        """Initialize for a specific w parameter and window ID.

        Args:
            w: Window parameter (2^w windows total)
            query_window_id: Current window ID for querying
        """
        self.w = w
        self.query_window_id = query_window_id
        self.window_count = 2 ** w

    @staticmethod
    def compute_from_event_id(event_id_bytes: bytes, w: int) -> int:
        """Compute window ID for an event at given w parameter.

        Uses top w bits of BLAKE2b-256 hash of event_id.

        Args:
            event_id_bytes: Raw event ID bytes
            w: Window parameter (bits to extract)

        Returns:
            Window ID in range [0, 2^w)
        """
        h = hashlib.blake2b(event_id_bytes, digest_size=32)
        hash_int = int.from_bytes(h.digest(), byteorder='big')
        return hash_int >> (256 - w)

    @staticmethod
    def storage_window_from_event_id(event_id_bytes: bytes) -> int:
        """Compute storage window (always uses STORAGE_W=20).

        Storage windows provide high granularity for large event stores.

        Args:
            event_id_bytes: Raw event ID bytes

        Returns:
            Storage window ID in range [0, 2^20)
        """
        return SyncWindow.compute_from_event_id(event_id_bytes, STORAGE_W)

    @staticmethod
    def optimal_w_for_event_count(total_events: int) -> int:
        """Compute optimal w parameter for given event count.

        Targets approximately EVENTS_PER_WINDOW_TARGET events per window
        for good false positive rate in bloom filters.

        Args:
            total_events: Total number of events

        Returns:
            Optimal w parameter (2^w windows)
        """
        if total_events == 0:
            return DEFAULT_W

        target_windows = max(1, total_events // EVENTS_PER_WINDOW_TARGET)
        return max(1, math.ceil(math.log2(target_windows)))

    def get_storage_window_range(self) -> tuple[int, int]:
        """Get storage window range for this query window.

        Maps a query window at w=self.w to corresponding storage windows at w=STORAGE_W.
        This is used to determine which storage windows contain events for this query window.

        The query window ID is a "bucket" at lower granularity (w=12).
        Multiple storage windows (w=20) fit into each query window.
        Specifically: (2^STORAGE_W) / (2^self.w) = 2^(STORAGE_W - self.w) storage windows per query window.

        Returns:
            (min_storage_window, max_storage_window) - inclusive range
        """
        # Expand query window to storage granularity
        # window_min is inclusive, window_max is exclusive in range calculation
        window_min = self.query_window_id << (STORAGE_W - self.w)
        window_max = (self.query_window_id + 1) << (STORAGE_W - self.w)
        return window_min, window_max

    def next_window(self, current_window: int) -> int:
        """Get next window in traversal order.

        Computes the next window to sync after syncing the current_window.
        Windows wrap around modulo window_count.

        Args:
            current_window: Last synced window (-1 if starting)

        Returns:
            Next window ID to sync
        """
        return (current_window + 1) % self.window_count

    def walk_all(self, start_after: int = -1) -> Iterator[int]:
        """Generate all windows in order starting after given window.

        This iterator generates the complete sequence of windows to sync,
        wrapping around from the highest window to zero.

        Args:
            start_after: Window to start after (-1 for beginning, which yields window 0 first)

        Yields:
            Window IDs in traversal order
        """
        start = (start_after + 1) % self.window_count
        for i in range(self.window_count):
            yield (start + i) % self.window_count
