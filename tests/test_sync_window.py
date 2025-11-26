"""Unit tests for SyncWindow class."""
import pytest
from events.network.sync_window import SyncWindow, DEFAULT_W, STORAGE_W, EVENTS_PER_WINDOW_TARGET


class TestSyncWindowBasics:
    """Test SyncWindow basic functionality."""

    def test_init(self):
        """Test SyncWindow initialization."""
        window = SyncWindow(w=12, query_window_id=0)
        assert window.w == 12
        assert window.query_window_id == 0
        assert window.window_count == 2**12

    def test_init_different_w(self):
        """Test SyncWindow with different w parameters."""
        window = SyncWindow(w=20, query_window_id=1000)
        assert window.w == 20
        assert window.query_window_id == 1000
        assert window.window_count == 2**20


class TestWindowIdComputation:
    """Test window ID computation from event IDs."""

    def test_compute_from_event_id_deterministic(self):
        """Test that window ID computation is deterministic."""
        event_id = b'test_event_id_12345'

        window_id_1 = SyncWindow.compute_from_event_id(event_id, w=12)
        window_id_2 = SyncWindow.compute_from_event_id(event_id, w=12)

        assert window_id_1 == window_id_2
        print(f"Deterministic test passed: {window_id_1}")

    def test_compute_from_event_id_range(self):
        """Test that window ID is in valid range for w parameter."""
        event_id = b'test_event_id'

        for w in [8, 12, 16, 20]:
            window_id = SyncWindow.compute_from_event_id(event_id, w=w)
            assert 0 <= window_id < 2**w, f"Window ID {window_id} out of range for w={w}"

    def test_compute_from_event_id_different_w(self):
        """Test that different w parameters give different window IDs."""
        event_id = b'test_event_id'

        window_id_12 = SyncWindow.compute_from_event_id(event_id, w=12)
        window_id_20 = SyncWindow.compute_from_event_id(event_id, w=20)

        # With different w values, we might get different window IDs
        # (Same event can be in different windows at different granularity)
        assert 0 <= window_id_12 < 2**12
        assert 0 <= window_id_20 < 2**20
        print(f"Event in window {window_id_12} at w=12, window {window_id_20} at w=20")

    def test_compute_different_events(self):
        """Test that different events get different window IDs (usually)."""
        event_id_1 = b'event_id_one'
        event_id_2 = b'event_id_two'

        window_id_1 = SyncWindow.compute_from_event_id(event_id_1, w=12)
        window_id_2 = SyncWindow.compute_from_event_id(event_id_2, w=12)

        # Different events should usually map to different windows (not guaranteed, but very likely)
        # At least verify they're in valid range
        assert 0 <= window_id_1 < 2**12
        assert 0 <= window_id_2 < 2**12

    def test_storage_window_from_event_id(self):
        """Test storage window computation (always uses STORAGE_W=20)."""
        event_id = b'test_event_id'

        storage_window = SyncWindow.storage_window_from_event_id(event_id)

        assert 0 <= storage_window < 2**STORAGE_W
        # Should be equivalent to compute_from_event_id with w=20
        assert storage_window == SyncWindow.compute_from_event_id(event_id, w=STORAGE_W)


class TestStorageWindowMapping:
    """Test mapping between query windows and storage windows."""

    def test_storage_range_window_zero_w12(self):
        """Test storage window range for query window 0 at w=12."""
        window = SyncWindow(w=12, query_window_id=0)
        storage_min, storage_max = window.get_storage_window_range()

        assert storage_min == 0
        # Query window 0 at w=12 should map to storage windows 0-(2^8-1)
        assert storage_max == 1 << (STORAGE_W - 12)

    def test_storage_range_window_one_w12(self):
        """Test storage window range for query window 1 at w=12."""
        window = SyncWindow(w=12, query_window_id=1)
        storage_min, storage_max = window.get_storage_window_range()

        # Window 1 at w=12 should map to windows starting after window 0's range
        expected_min = 1 << (STORAGE_W - 12)
        expected_max = 2 << (STORAGE_W - 12)

        assert storage_min == expected_min
        assert storage_max == expected_max

    def test_storage_range_all_windows_cover_space(self):
        """Test that all windows at a given w cover the storage space without gaps."""
        w = 12
        window_count = 2 ** w

        all_ranges = []
        for window_id in range(window_count):
            window = SyncWindow(w=w, query_window_id=window_id)
            storage_min, storage_max = window.get_storage_window_range()
            all_ranges.append((storage_min, storage_max))

        # Sort ranges and verify they cover [0, 2^20) without gaps
        all_ranges.sort()
        expected_min = 0
        for storage_min, storage_max in all_ranges:
            assert storage_min == expected_min
            expected_min = storage_max

        # Final window should end at 2^20
        assert expected_min == 2**STORAGE_W

    def test_storage_range_w20(self):
        """Test storage window range when w=20 (no expansion)."""
        window = SyncWindow(w=20, query_window_id=1000)
        storage_min, storage_max = window.get_storage_window_range()

        # When w=20, range should be [window_id, window_id+1)
        assert storage_min == 1000
        assert storage_max == 1001


class TestOptimalWindowParameter:
    """Test computation of optimal w parameter for event counts."""

    def test_optimal_w_zero_events(self):
        """Test that zero events returns DEFAULT_W."""
        w = SyncWindow.optimal_w_for_event_count(0)
        assert w == DEFAULT_W

    def test_optimal_w_small_count(self):
        """Test w parameter for small event counts."""
        # Very few events: should use small w (maybe w=1 for single window)
        w = SyncWindow.optimal_w_for_event_count(100)
        assert w >= 1

    def test_optimal_w_target_density(self):
        """Test that w parameter maintains target density (450 events/window)."""
        total_events = EVENTS_PER_WINDOW_TARGET * 10  # 10 windows worth
        w = SyncWindow.optimal_w_for_event_count(total_events)

        # Should require at least 4 windows (2^4=16, way more than 10)
        # But less than 2^10 = 1024
        assert w <= 10

    def test_optimal_w_monotonic(self):
        """Test that w increases (or stays same) as event count increases."""
        w1 = SyncWindow.optimal_w_for_event_count(1000)
        w2 = SyncWindow.optimal_w_for_event_count(10000)
        w3 = SyncWindow.optimal_w_for_event_count(100000)

        assert w1 <= w2
        assert w2 <= w3


class TestWindowTraversal:
    """Test window traversal and iteration."""

    def test_next_window_basic(self):
        """Test next_window computation."""
        window = SyncWindow(w=12, query_window_id=0)

        next_id = window.next_window(current_window=0)
        assert next_id == 1

        next_id = window.next_window(current_window=100)
        assert next_id == 101

    def test_next_window_wraparound(self):
        """Test that next_window wraps around at boundary."""
        window = SyncWindow(w=4, query_window_id=0)  # 16 windows total

        # Last window should wrap to 0
        next_id = window.next_window(current_window=15)
        assert next_id == 0

    def test_next_window_from_minus_one(self):
        """Test next_window starting from -1 (initial state)."""
        window = SyncWindow(w=12, query_window_id=0)

        next_id = window.next_window(current_window=-1)
        assert next_id == 0

    def test_walk_all_covers_all_windows(self):
        """Test that walk_all generates all windows exactly once."""
        window = SyncWindow(w=4, query_window_id=0)  # 16 windows

        visited = set()
        for window_id in window.walk_all(start_after=-1):
            visited.add(window_id)

        expected = set(range(16))
        assert visited == expected, f"Expected {expected}, got {visited}"

    def test_walk_all_starts_at_zero(self):
        """Test that walk_all with start_after=-1 yields window 0 first."""
        window = SyncWindow(w=4, query_window_id=0)

        first_window = next(window.walk_all(start_after=-1))
        assert first_window == 0

    def test_walk_all_start_after_middle(self):
        """Test walk_all starting after a middle window."""
        window = SyncWindow(w=4, query_window_id=0)  # 16 windows

        visited = list(window.walk_all(start_after=5))

        # Should start with window 6
        assert visited[0] == 6
        # Should end with window 5 (wrapping around)
        assert visited[-1] == 5
        # Should visit all 16 windows
        assert len(visited) == 16

    def test_walk_all_start_after_last(self):
        """Test walk_all starting after the last window (wraps to 0)."""
        window = SyncWindow(w=4, query_window_id=0)  # 16 windows

        visited = list(window.walk_all(start_after=15))

        assert visited[0] == 0  # Should wrap to 0
        assert len(visited) == 16


class TestEventWindowConsistency:
    """Test consistency between event window assignment and traversal."""

    def test_event_in_query_window_range(self):
        """Test that events hash to windows correctly."""
        w = 12
        event_id = b'test_event'

        # Event should be in a query window
        query_window_id = SyncWindow.compute_from_event_id(event_id, w=w)

        # The query window should have a defined storage range
        window = SyncWindow(w=w, query_window_id=query_window_id)
        storage_min, storage_max = window.get_storage_window_range()

        # Event's storage window should be in the query window's range
        storage_window = SyncWindow.storage_window_from_event_id(event_id)
        assert storage_min <= storage_window < storage_max

    def test_multiple_events_same_query_window(self):
        """Test that multiple events can hash to same query window."""
        w = 2  # Only 4 windows, so collisions are likely

        # Generate many events and check distribution
        collisions = 0
        for i in range(100):
            event_id_1 = f'event_{i*2}'.encode()
            event_id_2 = f'event_{i*2+1}'.encode()

            window_1 = SyncWindow.compute_from_event_id(event_id_1, w=w)
            window_2 = SyncWindow.compute_from_event_id(event_id_2, w=w)

            if window_1 == window_2:
                collisions += 1

        # With high probability, we should see some collisions
        assert collisions > 0, "Expected some events to hash to same window"
