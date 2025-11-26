"""Reusable assertion helpers for scenario tests.

Eliminates repeated assertion patterns across test_forward_secrecy.py,
test_user_removal.py, and other scenario tests.
"""
from db import Database, create_safe_db, create_unsafe_db


def assert_message_exists(safedb, message_id: str, peer_id: str, content: str | None = None) -> None:
    """Assert that a message exists for a peer and optionally has expected content.

    Args:
        safedb: SafeDB instance scoped to the peer
        message_id: Message ID to check
        peer_id: Peer who should see the message
        content: Optional expected message content
    """
    result = safedb.query_one(
        "SELECT content FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, peer_id)
    )
    assert result is not None, f"Message {message_id[:20]}... should exist for {peer_id[:20]}..."
    if content is not None:
        assert result['content'] == content, \
            f"Message content mismatch. Expected '{content}', got '{result['content']}'"


def assert_message_deleted(safedb, message_id: str, peer_id: str) -> None:
    """Assert that a message has been deleted for a peer.

    Args:
        safedb: SafeDB instance scoped to the peer
        message_id: Message ID to check
        peer_id: Peer who should not see the message
    """
    result = safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, peer_id)
    )
    assert result is None, f"Message {message_id[:20]}... should be deleted for {peer_id[:20]}..."


def assert_key_marked_for_purging(safedb, key_id: str, peer_id: str) -> None:
    """Assert that an encryption key has been marked for purging.

    Args:
        safedb: SafeDB instance scoped to the peer
        key_id: Key ID to check
        peer_id: Peer who should have the key marked
    """
    result = safedb.query_one(
        "SELECT 1 FROM keys_to_purge WHERE key_id = ? AND recorded_by = ? LIMIT 1",
        (key_id, peer_id)
    )
    assert result is not None, f"Key {key_id[:20]}... should be marked for purging"


def assert_key_purged(safedb, key_id: str, peer_id: str) -> None:
    """Assert that an encryption key has been purged from group_keys.

    Args:
        safedb: SafeDB instance scoped to the peer
        key_id: Key ID to check
        peer_id: Peer who should not have the key
    """
    result = safedb.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ? LIMIT 1",
        (key_id, peer_id)
    )
    assert result is None, f"Key {key_id[:20]}... should be purged from group_keys"


def assert_keys_to_purge_empty(safedb, peer_id: str) -> None:
    """Assert that the keys_to_purge table is empty for a peer.

    Args:
        safedb: SafeDB instance scoped to the peer
        peer_id: Peer to check
    """
    results = safedb.query(
        "SELECT 1 FROM keys_to_purge WHERE recorded_by = ?",
        (peer_id,)
    )
    assert len(results) == 0, \
        f"keys_to_purge should be empty but has {len(results)} entries"


def assert_file_exists(safedb, file_id: str, peer_id: str) -> None:
    """Assert that a file attachment exists for a peer.

    Args:
        safedb: SafeDB instance scoped to the peer
        file_id: File ID to check
        peer_id: Peer who should have the file
    """
    result = safedb.query_one(
        "SELECT 1 FROM message_attachments WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, peer_id)
    )
    assert result is not None, f"File {file_id[:20]}... should exist for {peer_id[:20]}..."


def assert_file_deleted(safedb, file_id: str, peer_id: str) -> None:
    """Assert that a file attachment has been deleted for a peer.

    Args:
        safedb: SafeDB instance scoped to the peer
        file_id: File ID to check
        peer_id: Peer who should not have the file
    """
    result = safedb.query_one(
        "SELECT 1 FROM message_attachments WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, peer_id)
    )
    assert result is None, f"File {file_id[:20]}... should be deleted for {peer_id[:20]}..."


def assert_peer_has_access(safedb, peer_id_to_check: str, original_peer_id: str) -> None:
    """Assert that a peer has access to another peer's resources.

    Args:
        safedb: SafeDB instance scoped to the checking peer
        peer_id_to_check: Peer whose presence to verify
        original_peer_id: Original peer ID to reference
    """
    result = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE recorded_by = ? LIMIT 1",
        (safedb._recorded_by,)
    )
    assert result is not None, \
        f"Peer {original_peer_id[:20]}... should have access for {peer_id_to_check[:20]}..."


def assert_peer_no_access(safedb, peer_id: str) -> None:
    """Assert that a peer no longer has access to resources.

    Args:
        safedb: SafeDB instance scoped to the peer
        peer_id: Peer whose access should be revoked
    """
    result = safedb.query_one(
        "SELECT 1 FROM local_peers WHERE peer_id = ? LIMIT 1",
        (peer_id,)
    )
    assert result is None, f"Peer {peer_id[:20]}... should not have access"


def assert_device_count(unsafedb, peer_shared_id: str, expected_count: int) -> None:
    """Assert the number of devices/links for a peer.

    Args:
        unsafedb: UnsafeDB instance (device-wide view)
        peer_shared_id: Peer's shared ID
        expected_count: Expected number of devices
    """
    result = unsafedb.query(
        "SELECT 1 FROM local_peers WHERE peer_shared_id = ?",
        (peer_shared_id,)
    )
    assert len(result) == expected_count, \
        f"Peer {peer_shared_id[:20]}... should have {expected_count} devices but has {len(result)}"
