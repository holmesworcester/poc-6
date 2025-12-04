"""Test SQLite XOR storage patterns.

Explore the best way to store and compute XOR fingerprints in SQLite.
"""
import sqlite3
import hashlib
import time
import random
import string


def xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR two byte strings."""
    return bytes(x ^ y for x, y in zip(a, b))


def hash_id(event_id: str) -> bytes:
    """Hash event_id to 16 bytes."""
    return hashlib.blake2b(event_id.encode(), digest_size=16).digest()


ZERO_HASH = bytes(16)


# =============================================================================
# Test 1: Store XOR as BLOB, update with Python
# =============================================================================

def test_blob_storage():
    """Test storing XOR hashes as BLOBs."""
    print("\n=== Test: BLOB Storage ===")

    db = sqlite3.connect(":memory:")
    db.execute("""
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            item_hash BLOB NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE buckets (
            bucket_id INTEGER PRIMARY KEY,
            xor_hash BLOB NOT NULL,
            item_count INTEGER NOT NULL
        )
    """)

    # Initialize bucket
    db.execute("INSERT INTO buckets VALUES (0, ?, 0)", (ZERO_HASH,))

    # Insert items and update bucket
    n = 1000
    start = time.perf_counter()

    for i in range(n):
        event_id = f"event_{i:06d}"
        item_hash = hash_id(event_id)

        db.execute("INSERT INTO items VALUES (?, ?)", (event_id, item_hash))

        # Update bucket XOR (fetch, XOR in Python, update)
        row = db.execute("SELECT xor_hash FROM buckets WHERE bucket_id = 0").fetchone()
        new_xor = xor_bytes(row[0], item_hash)
        db.execute("UPDATE buckets SET xor_hash = ?, item_count = item_count + 1 WHERE bucket_id = 0",
                   (new_xor,))

    db.commit()
    elapsed = time.perf_counter() - start

    # Verify
    row = db.execute("SELECT xor_hash, item_count FROM buckets WHERE bucket_id = 0").fetchone()
    assert row[1] == n

    print(f"  Inserted {n} items in {elapsed*1000:.1f}ms ({n/elapsed:.0f} items/sec)")
    print(f"  Final XOR hash: {row[0].hex()[:16]}...")
    print("  ✓ PASS")


# =============================================================================
# Test 2: Custom SQLite aggregate function for XOR
# =============================================================================

class XorAggregate:
    """SQLite aggregate function for XOR."""

    def __init__(self):
        self.result = bytearray(16)

    def step(self, value):
        if value:
            for i in range(16):
                self.result[i] ^= value[i]

    def finalize(self):
        return bytes(self.result)


def test_xor_aggregate():
    """Test custom XOR aggregate function."""
    print("\n=== Test: Custom XOR Aggregate ===")

    db = sqlite3.connect(":memory:")
    db.create_aggregate("xor_agg", 1, XorAggregate)

    db.execute("""
        CREATE TABLE items (
            timestamp_ms INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            item_hash BLOB NOT NULL,
            PRIMARY KEY (timestamp_ms, event_id)
        )
    """)

    # Insert items
    n = 10000
    print(f"  Inserting {n} items...")
    start = time.perf_counter()

    for i in range(n):
        ts = 1000000 + i * 100
        event_id = f"event_{i:06d}"
        item_hash = hash_id(event_id)
        db.execute("INSERT INTO items VALUES (?, ?, ?)", (ts, event_id, item_hash))

    db.commit()
    insert_time = time.perf_counter() - start
    print(f"  Insert time: {insert_time*1000:.1f}ms")

    # Test full range XOR
    start = time.perf_counter()
    row = db.execute("SELECT xor_agg(item_hash) FROM items").fetchone()
    full_xor_time = time.perf_counter() - start
    print(f"  Full XOR ({n} items): {full_xor_time*1000:.1f}ms")

    # Test range XOR (middle 50%)
    start_ts = 1000000 + (n // 4) * 100
    end_ts = 1000000 + (3 * n // 4) * 100

    start = time.perf_counter()
    row = db.execute("""
        SELECT xor_agg(item_hash) FROM items
        WHERE timestamp_ms >= ? AND timestamp_ms < ?
    """, (start_ts, end_ts)).fetchone()
    range_xor_time = time.perf_counter() - start
    print(f"  Range XOR (50% = {n//2} items): {range_xor_time*1000:.1f}ms")

    # Test small range XOR (1%)
    start_ts = 1000000 + (n // 2) * 100
    end_ts = start_ts + (n // 100) * 100

    start = time.perf_counter()
    row = db.execute("""
        SELECT xor_agg(item_hash) FROM items
        WHERE timestamp_ms >= ? AND timestamp_ms < ?
    """, (start_ts, end_ts)).fetchone()
    small_xor_time = time.perf_counter() - start
    print(f"  Small XOR (1% = {n//100} items): {small_xor_time*1000:.1f}ms")

    print("  ✓ PASS")


# =============================================================================
# Test 3: Hierarchical buckets with stored XOR
# =============================================================================

def test_hierarchical_buckets():
    """Test hierarchical bucket storage for O(log n) queries."""
    print("\n=== Test: Hierarchical Buckets ===")

    db = sqlite3.connect(":memory:")
    db.create_aggregate("xor_agg", 1, XorAggregate)

    db.execute("""
        CREATE TABLE items (
            timestamp_ms INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            item_hash BLOB NOT NULL,
            bucket_0 INTEGER NOT NULL,
            PRIMARY KEY (timestamp_ms, event_id)
        )
    """)

    # Level 0: 64 buckets, Level 1: 8 buckets, Level 2: 1 bucket (root)
    db.execute("""
        CREATE TABLE buckets (
            level INTEGER NOT NULL,
            bucket_id INTEGER NOT NULL,
            xor_hash BLOB NOT NULL,
            item_count INTEGER NOT NULL,
            PRIMARY KEY (level, bucket_id)
        )
    """)

    # Initialize bucket hierarchy
    for level in range(3):
        num_buckets = 64 >> (level * 3)  # 64, 8, 1
        for bid in range(num_buckets):
            db.execute("INSERT INTO buckets VALUES (?, ?, ?, 0)",
                       (level, bid, ZERO_HASH))

    db.commit()

    # Insert items with bucket updates
    n = 10000
    bucket_size = n // 64  # ~156 items per level-0 bucket

    print(f"  Inserting {n} items with bucket updates...")
    start = time.perf_counter()

    for i in range(n):
        ts = 1000000 + i * 100
        event_id = f"event_{i:06d}"
        item_hash = hash_id(event_id)

        # Compute bucket IDs
        bucket_0 = i // bucket_size
        if bucket_0 >= 64:
            bucket_0 = 63

        db.execute("INSERT INTO items VALUES (?, ?, ?, ?)",
                   (ts, event_id, item_hash, bucket_0))

        # Update bucket hierarchy (3 levels)
        for level in range(3):
            bid = bucket_0 >> (level * 3)
            row = db.execute("SELECT xor_hash FROM buckets WHERE level = ? AND bucket_id = ?",
                             (level, bid)).fetchone()
            new_xor = xor_bytes(row[0], item_hash)
            db.execute("""
                UPDATE buckets SET xor_hash = ?, item_count = item_count + 1
                WHERE level = ? AND bucket_id = ?
            """, (new_xor, level, bid))

    db.commit()
    insert_time = time.perf_counter() - start
    print(f"  Insert time: {insert_time*1000:.1f}ms ({n/insert_time:.0f} items/sec)")

    # Verify root bucket
    row = db.execute("SELECT xor_hash, item_count FROM buckets WHERE level = 2 AND bucket_id = 0").fetchone()
    print(f"  Root bucket: {row[1]} items, hash={row[0].hex()[:16]}...")

    # Compare: aggregate vs stored bucket
    start = time.perf_counter()
    agg_row = db.execute("SELECT xor_agg(item_hash) FROM items").fetchone()
    agg_time = time.perf_counter() - start

    assert row[0] == agg_row[0], "Root bucket hash should match aggregate!"
    print(f"  Verify via aggregate: {agg_time*1000:.1f}ms (matches ✓)")

    # Test range query using buckets
    # Query buckets 16-47 (half the data)
    start = time.perf_counter()
    bucket_xor = ZERO_HASH
    for bid in range(16, 48):
        row = db.execute("SELECT xor_hash FROM buckets WHERE level = 0 AND bucket_id = ?", (bid,)).fetchone()
        bucket_xor = xor_bytes(bucket_xor, row[0])
    bucket_time = time.perf_counter() - start

    # Compare with aggregate over same range
    start_ts = 1000000 + 16 * bucket_size * 100
    end_ts = 1000000 + 48 * bucket_size * 100

    start = time.perf_counter()
    agg_row = db.execute("""
        SELECT xor_agg(item_hash) FROM items
        WHERE bucket_0 >= 16 AND bucket_0 < 48
    """).fetchone()
    agg_range_time = time.perf_counter() - start

    assert bucket_xor == agg_row[0], "Bucket range should match aggregate!"
    print(f"  Range query (50%): buckets={bucket_time*1000:.2f}ms, aggregate={agg_range_time*1000:.1f}ms")
    print(f"  Speedup: {agg_range_time/bucket_time:.1f}x")

    print("  ✓ PASS")


# =============================================================================
# Test 4: SQLite built-in functions for XOR
# =============================================================================

def test_sqlite_builtins():
    """Test SQLite version and note XOR limitations."""
    print("\n=== Test: SQLite Built-in XOR ===")

    db = sqlite3.connect(":memory:")

    row = db.execute("SELECT sqlite_version()").fetchone()
    print(f"  SQLite version: {row[0]}")

    print("  Note: SQLite has no built-in XOR for BLOBs")
    print("  Best approach: Custom aggregate function (XorAggregate)")
    print("  For O(log n): Hierarchical buckets with stored XOR hashes")
    print("  ✓ PASS")


# =============================================================================
# Test 5: Performance at scale
# =============================================================================

def test_performance_at_scale():
    """Test performance with larger dataset."""
    print("\n=== Test: Performance at Scale ===")

    n = 50000
    print(f"  Dataset: {n} items")

    db = sqlite3.connect(":memory:")
    db.create_aggregate("xor_agg", 1, XorAggregate)
    db.execute("""
        CREATE TABLE items (
            timestamp_ms INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            item_hash BLOB NOT NULL,
            PRIMARY KEY (timestamp_ms, event_id)
        )
    """)

    # Insert
    print("  Inserting...")
    items = []
    for i in range(n):
        ts = 1000000 + i * 100
        event_id = f"event_{i:06d}"
        item_hash = hash_id(event_id)
        items.append((ts, event_id, item_hash))

    start = time.perf_counter()
    db.executemany("INSERT INTO items VALUES (?, ?, ?)", items)
    db.commit()
    insert_time = time.perf_counter() - start
    print(f"  Insert: {insert_time*1000:.1f}ms ({n/insert_time:.0f} items/sec)")

    # Full XOR
    start = time.perf_counter()
    for _ in range(10):
        db.execute("SELECT xor_agg(item_hash) FROM items").fetchone()
    full_time = (time.perf_counter() - start) / 10
    print(f"  Full XOR ({n} items): {full_time*1000:.1f}ms")

    # Range XOR (50%)
    start_ts = 1000000 + (n // 4) * 100
    end_ts = 1000000 + (3 * n // 4) * 100

    start = time.perf_counter()
    for _ in range(10):
        db.execute("""
            SELECT xor_agg(item_hash) FROM items
            WHERE timestamp_ms >= ? AND timestamp_ms < ?
        """, (start_ts, end_ts)).fetchone()
    range_time = (time.perf_counter() - start) / 10
    print(f"  Range XOR (50% = {n//2} items): {range_time*1000:.1f}ms")

    # Small range (1%)
    start_ts = 1000000 + (n // 2) * 100
    end_ts = start_ts + (n // 100) * 100

    start = time.perf_counter()
    for _ in range(100):
        db.execute("""
            SELECT xor_agg(item_hash) FROM items
            WHERE timestamp_ms >= ? AND timestamp_ms < ?
        """, (start_ts, end_ts)).fetchone()
    small_time = (time.perf_counter() - start) / 100
    print(f"  Small XOR (1% = {n//100} items): {small_time*1000:.2f}ms")

    print("  ✓ PASS")


if __name__ == "__main__":
    test_blob_storage()
    test_xor_aggregate()
    test_hierarchical_buckets()
    test_sqlite_builtins()
    test_performance_at_scale()

    print("\n" + "="*50)
    print("All SQLite XOR tests passed!")
