"""Tests for SafeDB and UnsafeDB scoping enforcement.

These tests verify that:
1. All subjective tables have recorded_by in their PRIMARY KEY
2. SafeDB enforces scoping and prevents data leakage between peers
3. UnsafeDB only allows access to device tables
4. Runtime validation catches scoping violations
"""
import sqlite3
import pytest
from db import (
    Database,
    create_safe_db,
    create_unsafe_db,
    ScopingViolation,
    SUBJECTIVE_TABLES,
    DEVICE_TABLES,
)
import schema


def test_subjective_tables_have_recorded_by_in_pk():
    """Verify all subjective tables have recorded_by in PRIMARY KEY."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    for table in SUBJECTIVE_TABLES:
        # Get PRIMARY KEY columns using pragma_table_info
        pk_info = db.query(f"PRAGMA table_info({table})")
        pk_cols = [row['name'] for row in pk_info if row['pk'] > 0]

        # Special case: shareable_events uses can_share_peer_id instead of recorded_by
        if table == 'shareable_events':
            assert 'can_share_peer_id' in pk_cols, \
                f"Table {table} missing can_share_peer_id in PRIMARY KEY {pk_cols}"
        else:
            assert 'recorded_by' in pk_cols, \
                f"Table {table} missing recorded_by in PRIMARY KEY {pk_cols}"


def test_peer_view_isolation():
    """Verify that peers have isolated views and cannot see each other's data."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice_id = 'alice_peer_id'
    bob_id = 'bob_peer_id'

    alice_db = create_safe_db(db, recorded_by=alice_id)
    bob_db = create_safe_db(db, recorded_by=bob_id)

    # Alice inserts a message
    alice_db.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('msg1', 'chan1', 'grp1', alice_id, 'Alice secret', 1000, 0, alice_id, 1001)
    )

    # Bob inserts a message with same message_id (different peer view)
    bob_db.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('msg1', 'chan1', 'grp1', bob_id, 'Bob secret', 2000, 0, bob_id, 2001)
    )

    db.commit()

    # Alice can only see her own message
    alice_msgs = alice_db.query(
        "SELECT * FROM messages WHERE message_id = ? AND recorded_by = ?",
        ('msg1', alice_id)
    )
    assert len(alice_msgs) == 1
    assert alice_msgs[0]['content'] == 'Alice secret'
    assert alice_msgs[0]['recorded_by'] == alice_id

    # Bob can only see his own message
    bob_msgs = bob_db.query(
        "SELECT * FROM messages WHERE message_id = ? AND recorded_by = ?",
        ('msg1', bob_id)
    )
    assert len(bob_msgs) == 1
    assert bob_msgs[0]['content'] == 'Bob secret'
    assert bob_msgs[0]['recorded_by'] == bob_id

    # Bob cannot see Alice's message (SafeDB should catch this as scoping violation)
    with pytest.raises(ScopingViolation, match="SCOPING VIOLATION.*alice_peer_id"):
        bob_db.query(
            "SELECT * FROM messages WHERE message_id = ? AND recorded_by = ?",
            ('msg1', alice_id)  # Trying to query Alice's data - this should fail!
        )


def test_safedb_rejects_device_tables():
    """SafeDB should reject operations on device tables."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    safedb = create_safe_db(db, recorded_by='alice')

    # Should reject query on device table
    with pytest.raises(ScopingViolation, match="SafeDB can only access subjective tables"):
        safedb.query("SELECT * FROM local_peers")

    # Should reject insert on device table
    with pytest.raises(ScopingViolation, match="SafeDB can only access subjective tables"):
        safedb.execute("INSERT INTO store VALUES (?, ?, ?)", (b'id', b'blob', 1000))


def test_unsafedb_rejects_subjective_tables():
    """UnsafeDB should reject operations on subjective tables."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    unsafedb = create_unsafe_db(db)

    # Should reject query on subjective table
    with pytest.raises(ScopingViolation, match="UnsafeDB cannot access subjective tables"):
        unsafedb.query("SELECT * FROM messages")

    # Should reject insert on subjective table
    with pytest.raises(ScopingViolation, match="UnsafeDB cannot access subjective tables"):
        unsafedb.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ('msg1', 'chan1', 'grp1', 'alice', 'content', 1000, 'alice', 1001)
        )


def test_safedb_validates_query_has_recorded_by_filter():
    """SafeDB should reject queries that don't filter by recorded_by."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    safedb = create_safe_db(db, recorded_by='alice')

    # Should reject query without recorded_by filter
    with pytest.raises(ScopingViolation, match="must include recorded_by"):
        safedb.query("SELECT * FROM messages WHERE channel_id = ?", ('chan1',))


def test_safedb_catches_wrong_recorded_by_in_results():
    """SafeDB should catch if query returns rows with wrong recorded_by."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice_id = 'alice_peer_id'
    bob_id = 'bob_peer_id'

    # Insert data for both peers using raw db
    db.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('msg1', 'chan1', 'grp1', alice_id, 'Alice msg', 1000, 0, alice_id, 1001)
    )
    db.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('msg2', 'chan1', 'grp1', bob_id, 'Bob msg', 2000, 0, bob_id, 2001)
    )
    db.commit()

    alice_db = create_safe_db(db, recorded_by=alice_id)

    # This query has recorded_by in WHERE but with wrong value
    # SafeDB should catch that returned rows don't match alice_id
    with pytest.raises(ScopingViolation, match="SCOPING VIOLATION.*recorded_by=bob_peer_id"):
        alice_db.query(
            "SELECT * FROM messages WHERE recorded_by = ?",
            (bob_id,)  # Wrong peer!
        )


def test_safedb_insert_validation():
    """SafeDB should validate that INSERTs include the correct recorded_by."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice_db = create_safe_db(db, recorded_by='alice')

    # Should reject INSERT without recorded_by value
    with pytest.raises(ScopingViolation, match="must include recorded_by"):
        alice_db.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ('msg1', 'chan1', 'grp1', 'author', 'content', 1000, 0, 'bob', 1001)  # Wrong peer!
        )

    # Should accept INSERT with correct recorded_by
    alice_db.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('msg1', 'chan1', 'grp1', 'author', 'content', 1000, 0, 'alice', 1001)
    )
    db.commit()

    # Verify it was inserted
    result = alice_db.query(
        "SELECT * FROM messages WHERE message_id = ? AND recorded_by = ?",
        ('msg1', 'alice')
    )
    assert len(result) == 1


def test_unsafedb_allows_device_operations():
    """UnsafeDB should allow normal operations on device tables."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    unsafedb = create_unsafe_db(db)

    # Should allow insert into device table
    unsafedb.execute(
        "INSERT INTO local_peers VALUES (?, ?, ?, ?)",
        ('peer1', 'pubkey1', b'privkey1', 1000)
    )
    db.commit()

    # Should allow query on device table
    peers = unsafedb.query("SELECT * FROM local_peers WHERE peer_id = ?", ('peer1',))
    assert len(peers) == 1
    assert peers[0]['peer_id'] == 'peer1'


def test_shareable_events_uses_can_share_peer_id():
    """Verify shareable_events table uses can_share_peer_id for scoping."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice_id = 'alice_peer_id'
    alice_db = create_safe_db(db, recorded_by=alice_id)

    # Insert into shareable_events (uses can_share_peer_id)
    alice_db.execute(
        "INSERT INTO shareable_events VALUES (?, ?, ?, ?, ?)",
        ('event1', alice_id, 1000, 1001, 5)
    )
    db.commit()

    # Query should work with can_share_peer_id filter
    results = alice_db.query(
        "SELECT * FROM shareable_events WHERE can_share_peer_id = ?",
        (alice_id,)
    )
    assert len(results) == 1
    assert results[0]['can_share_peer_id'] == alice_id


def test_users_and_group_members_have_scoping():
    """Verify users and group_members tables have proper recorded_by scoping."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice_id = 'alice_peer_id'
    bob_id = 'bob_peer_id'

    alice_db = create_safe_db(db, recorded_by=alice_id)
    bob_db = create_safe_db(db, recorded_by=bob_id)

    # Alice sees a user
    alice_db.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ('user1', 'peer1', 'User One', 'net1', 1000, 'invite_key', alice_id, 1001)
    )

    # Bob sees same user_id but different data
    bob_db.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ('user1', 'peer1', 'Different Name', 'net1', 2000, 'other_key', bob_id, 2001)
    )

    db.commit()

    # Alice and Bob have different views
    alice_user = alice_db.query_one(
        "SELECT * FROM users WHERE user_id = ? AND recorded_by = ?",
        ('user1', alice_id)
    )
    assert alice_user['name'] == 'User One'

    bob_user = bob_db.query_one(
        "SELECT * FROM users WHERE user_id = ? AND recorded_by = ?",
        ('user1', bob_id)
    )
    assert bob_user['name'] == 'Different Name'


def test_no_direct_db_access():
    """Lint check: Ensure no direct db.query/execute calls in main codebase.

    All code should use create_safe_db() or create_unsafe_db() to enforce scoping.
    """
    import glob
    import re

    # Files that are allowed to use raw db access
    ALLOWED_FILES = {
        'db.py',  # Internal implementation
        'tests/test_safe_scoping.py',  # This test file (uses raw db intentionally)
        'schema.py',  # Schema creation
    }

    # Directories to exclude
    EXCLUDED_DIRS = {'previous-poc', 'debug', '.pytest_cache', 'venv', '__pycache__', 'tests'}

    violations = []

    # Find all Python files
    for py_file in glob.glob('**/*.py', recursive=True):
        # Skip excluded directories
        if any(excluded in py_file for excluded in EXCLUDED_DIRS):
            continue

        # Skip debug and test scripts
        if 'debug_' in py_file or 'trace_' in py_file or 'check_' in py_file or 'test_' in py_file or 'analyze_' in py_file:
            continue

        # Skip allowed files
        if any(allowed in py_file for allowed in ALLOWED_FILES):
            continue

        # Read file and check for direct db access
        try:
            with open(py_file) as f:
                for line_num, line in enumerate(f, 1):
                    # Look for db.query(), db.execute(), db.query_one(), db.query_all()
                    if re.search(r'\bdb\.(query|execute|query_one|query_all|execute_returning)\(', line):
                        # Skip comments
                        if line.strip().startswith('#'):
                            continue
                        violations.append(f"{py_file}:{line_num}: {line.strip()}")
        except Exception as e:
            # Skip files that can't be read
            pass

    if violations:
        error_msg = (
            f"Found {len(violations)} direct db access violations.\n"
            f"Use create_safe_db(db, recorded_by=...) or create_unsafe_db(db) instead.\n\n"
            + "\n".join(violations[:20])  # Show first 20
        )
        if len(violations) > 20:
            error_msg += f"\n... and {len(violations) - 20} more"

        pytest.fail(error_msg)


def test_safedb_get_blob_enforces_scoping():
    """SafeDB.get_blob() should only return blobs for events in valid_events."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice_id = 'alice_peer_id'
    bob_id = 'bob_peer_id'

    # Create some test blobs in store
    import crypto
    alice_blob = b'alice secret data'
    bob_blob = b'bob secret data'

    alice_event_id = crypto.b64encode(crypto.hash(alice_blob))
    bob_event_id = crypto.b64encode(crypto.hash(bob_blob))

    # Store blobs (device-wide) - use base64 IDs (TEXT type)
    db.execute("INSERT INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
               (alice_event_id, alice_blob, 1000))
    db.execute("INSERT INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
               (bob_event_id, bob_blob, 2000))

    # Mark Alice's event as valid for Alice only
    db.execute("INSERT INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
               (alice_event_id, alice_id))

    # Mark Bob's event as valid for Bob only
    db.execute("INSERT INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
               (bob_event_id, bob_id))

    db.commit()

    alice_db = create_safe_db(db, recorded_by=alice_id)
    bob_db = create_safe_db(db, recorded_by=bob_id)

    # Alice can get her own blob
    alice_result = alice_db.get_blob(alice_event_id)
    assert alice_result == alice_blob

    # Bob can get his own blob
    bob_result = bob_db.get_blob(bob_event_id)
    assert bob_result == bob_blob

    # Alice CANNOT get Bob's blob (should raise ScopingViolation)
    with pytest.raises(ScopingViolation, match="doesn't have access"):
        alice_db.get_blob(bob_event_id)

    # Bob CANNOT get Alice's blob (should raise ScopingViolation)
    with pytest.raises(ScopingViolation, match="doesn't have access"):
        bob_db.get_blob(alice_event_id)


def test_safedb_get_blob_nonexistent_event():
    """SafeDB.get_blob() should raise ScopingViolation for nonexistent events."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice_id = 'alice_peer_id'
    alice_db = create_safe_db(db, recorded_by=alice_id)

    # Try to get a blob that doesn't exist at all
    # Use a valid base64 string that represents a non-existent event
    import crypto
    fake_event_id = crypto.b64encode(b'nonexistent_event_id_bytes')

    with pytest.raises(ScopingViolation, match="doesn't have access"):
        alice_db.get_blob(fake_event_id)
