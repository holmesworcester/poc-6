"""
Test file data URI and base64 upload/download functions.

Tests:
- Round-trip: upload base64 → download as data URI → verify match
- Different mime types (image/png, text/plain, application/pdf)
- Different file sizes (1KB, 100KB, 1MB)
- Invalid data URIs (malformed base64, missing mime type)
- Incomplete files (should return None)
- Metadata extraction
"""
import sqlite3
import base64
import pytest
from db import Database
import schema
from events.identity import user
from events.content import message, message_attachment


def test_roundtrip_base64_to_data_uri():
    """Test uploading via base64 and downloading as data URI."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create user
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Create message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test message',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']

    db.commit()

    # Create test file
    original_data = b'Hello, World! This is a test file.'
    original_base64 = base64.b64encode(original_data).decode('ascii')

    # Upload via base64
    upload_result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=original_base64,
        mime_type='text/plain',
        filename='test.txt',
        t_ms=3000,
        db=db
    )

    file_id = upload_result['file_id']
    assert file_id is not None
    assert upload_result['slice_count'] > 0

    db.commit()

    # Download as data URI
    data_uri = message_attachment.get_file_as_data_uri(
        file_id=file_id,
        recorded_by=alice['peer_id'],
        db=db
    )

    assert data_uri is not None
    assert data_uri.startswith('data:text/plain;base64,')

    # Extract base64 from data URI and decode
    _, base64_part = data_uri.split(',', 1)
    retrieved_data = base64.b64decode(base64_part)

    # Verify match
    assert retrieved_data == original_data
    print(f"✓ Round-trip successful: {len(original_data)} bytes")


def test_roundtrip_data_uri_to_data_uri():
    """Test uploading via full data URI and downloading."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Create original data URI
    original_data = b'PNG image data would go here...'
    original_base64 = base64.b64encode(original_data).decode('ascii')
    original_data_uri = f'data:image/png;base64,{original_base64}'

    # Upload via data URI
    upload_result = message_attachment.create_from_data_uri(
        peer_id=alice['peer_id'],
        message_id=message_id,
        data_uri=original_data_uri,
        filename='image.png',
        t_ms=3000,
        db=db
    )

    file_id = upload_result['file_id']
    db.commit()

    # Download as data URI
    retrieved_data_uri = message_attachment.get_file_as_data_uri(
        file_id=file_id,
        recorded_by=alice['peer_id'],
        db=db
    )

    assert retrieved_data_uri is not None
    assert retrieved_data_uri.startswith('data:image/png;base64,')

    # Verify data matches
    _, retrieved_base64 = retrieved_data_uri.split(',', 1)
    retrieved_data = base64.b64decode(retrieved_base64)
    assert retrieved_data == original_data

    print(f"✓ Data URI round-trip successful")


def test_different_mime_types():
    """Test various MIME types are preserved correctly."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    test_cases = [
        ('image/png', 'test.png', b'PNG data'),
        ('image/jpeg', 'photo.jpg', b'JPEG data'),
        ('application/pdf', 'document.pdf', b'PDF data'),
        ('text/plain', 'notes.txt', b'Text data'),
        ('application/json', 'config.json', b'{"key": "value"}'),
        ('video/mp4', 'video.mp4', b'MP4 data'),
    ]

    for mime_type, filename, file_data in test_cases:
        base64_data = base64.b64encode(file_data).decode('ascii')

        # Upload
        result = message_attachment.create_from_base64(
            peer_id=alice['peer_id'],
            message_id=message_id,
            base64_data=base64_data,
            mime_type=mime_type,
            filename=filename,
            t_ms=3000,
            db=db
        )
        file_id = result['file_id']
        db.commit()

        # Download
        data_uri = message_attachment.get_file_as_data_uri(
            file_id=file_id,
            recorded_by=alice['peer_id'],
            db=db
        )

        # Verify mime type
        assert data_uri.startswith(f'data:{mime_type};base64,'), \
            f"Expected {mime_type} but got {data_uri[:50]}"

        print(f"✓ MIME type {mime_type} preserved correctly")


def test_different_file_sizes():
    """Test various file sizes work correctly."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    test_sizes = [
        (100, "100 bytes"),
        (1 * 1024, "1 KB"),
        (10 * 1024, "10 KB"),
        (100 * 1024, "100 KB"),
        (1 * 1024 * 1024, "1 MB"),
    ]

    for size, label in test_sizes:
        file_data = b'X' * size
        base64_data = base64.b64encode(file_data).decode('ascii')

        # Upload
        result = message_attachment.create_from_base64(
            peer_id=alice['peer_id'],
            message_id=message_id,
            base64_data=base64_data,
            mime_type='application/octet-stream',
            filename=f'file_{label}.bin',
            t_ms=3000,
            db=db
        )
        file_id = result['file_id']
        db.commit()

        # Download
        data_uri = message_attachment.get_file_as_data_uri(
            file_id=file_id,
            recorded_by=alice['peer_id'],
            db=db
        )

        assert data_uri is not None

        # Verify size
        _, base64_part = data_uri.split(',', 1)
        retrieved_data = base64.b64decode(base64_part)
        assert len(retrieved_data) == size, \
            f"Expected {size} bytes but got {len(retrieved_data)}"

        print(f"✓ File size {label} ({size:,} bytes) works correctly")


def test_metadata_extraction():
    """Test that metadata is correctly extracted when requested."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Upload file
    file_data = b'Test file content here'
    base64_data = base64.b64encode(file_data).decode('ascii')

    result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=base64_data,
        mime_type='image/png',
        filename='photo.png',
        t_ms=3000,
        db=db
    )
    file_id = result['file_id']
    db.commit()

    # Get with metadata
    result = message_attachment.get_file_as_data_uri(
        file_id=file_id,
        recorded_by=alice['peer_id'],
        db=db,
        include_metadata=True
    )

    assert result is not None
    assert isinstance(result, dict)
    assert 'data_uri' in result
    assert 'filename' in result
    assert 'mime_type' in result
    assert 'size_bytes' in result
    assert 'size_human' in result

    assert result['filename'] == 'photo.png'
    assert result['mime_type'] == 'image/png'
    assert result['size_bytes'] == len(file_data)
    assert result['size_human'] == '22 B'
    assert result['data_uri'].startswith('data:image/png;base64,')

    print(f"✓ Metadata extraction works correctly")
    print(f"  - filename: {result['filename']}")
    print(f"  - mime_type: {result['mime_type']}")
    print(f"  - size: {result['size_human']}")


def test_invalid_base64():
    """Test that invalid base64 raises ValueError."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Invalid base64 (contains characters not in base64 alphabet)
    # With validate=True, base64.b64decode will reject invalid characters
    invalid_base64 = "This@is#not$valid%base64!"  # Contains @#$% which aren't in base64

    with pytest.raises(ValueError, match="Invalid base64 data"):
        message_attachment.create_from_base64(
            peer_id=alice['peer_id'],
            message_id=message_id,
            base64_data=invalid_base64,
            mime_type='text/plain',
            filename='test.txt',
            t_ms=3000,
            db=db
        )

    print("✓ Invalid base64 correctly raises ValueError")


def test_invalid_data_uri_format():
    """Test that invalid data URI formats raise ValueError."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    invalid_uris = [
        "http://example.com/file.png",  # Not a data URI
        "data:image/png",  # Missing comma separator
        "image/png;base64,SGVsbG8=",  # Missing 'data:' prefix
    ]

    for invalid_uri in invalid_uris:
        with pytest.raises(ValueError):
            message_attachment.create_from_data_uri(
                peer_id=alice['peer_id'],
                message_id=message_id,
                data_uri=invalid_uri,
                filename='test.png',
                t_ms=3000,
                db=db
            )

    print("✓ Invalid data URI formats correctly raise ValueError")


def test_incomplete_file_returns_none():
    """Test that get_file_as_data_uri returns None for incomplete files."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Upload file as alice
    file_data = b'Complete file data'
    base64_data = base64.b64encode(file_data).decode('ascii')

    result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=base64_data,
        mime_type='text/plain',
        filename='test.txt',
        t_ms=3000,
        db=db
    )
    file_id = result['file_id']
    db.commit()

    # Create a second user (Bob) who doesn't have the file slices yet
    from events.identity import invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=4000,
        db=db
    )
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=5000, db=db)
    db.commit()

    # Bob tries to get the file but doesn't have slices yet
    # First sync the metadata
    import tick
    for i in range(5):
        tick.tick(t_ms=6000 + i*100, db=db)
        db.commit()

    # Check if Bob has the metadata but not all slices
    # (The regular sync might have transferred some/all slices via opportunistic sync)
    # So we just verify the API returns None when slices are missing

    # Manually delete Bob's file slices to simulate incomplete download
    db.execute(
        "DELETE FROM file_slices WHERE file_id = ? AND recorded_by = ?",
        (file_id, bob['peer_id'])
    )
    db.commit()

    # Now Bob has metadata but no slices
    data_uri = message_attachment.get_file_as_data_uri(
        file_id=file_id,
        recorded_by=bob['peer_id'],
        db=db
    )

    # Should return None because file is incomplete
    assert data_uri is None

    print("✓ Incomplete file correctly returns None")


def test_default_mime_type():
    """Test that missing mime_type defaults to application/octet-stream."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Test',
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Upload without mime_type
    file_data = b'Unknown file type'
    base64_data = base64.b64encode(file_data).decode('ascii')

    result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=base64_data,
        mime_type=None,  # No mime type provided
        filename='mystery.bin',
        t_ms=3000,
        db=db
    )
    file_id = result['file_id']
    db.commit()

    # Download
    data_uri = message_attachment.get_file_as_data_uri(
        file_id=file_id,
        recorded_by=alice['peer_id'],
        db=db
    )

    # Should default to application/octet-stream
    assert data_uri.startswith('data:application/octet-stream;base64,')

    print("✓ Default mime_type correctly set to application/octet-stream")


if __name__ == '__main__':
    print("=" * 80)
    print("Running Data URI Tests")
    print("=" * 80)

    test_roundtrip_base64_to_data_uri()
    test_roundtrip_data_uri_to_data_uri()
    test_different_mime_types()
    test_different_file_sizes()
    test_metadata_extraction()
    test_invalid_base64()
    test_invalid_data_uri_format()
    test_incomplete_file_returns_none()
    test_default_mime_type()

    print("\n" + "=" * 80)
    print("ALL TESTS PASSED!")
    print("=" * 80)
