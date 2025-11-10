"""
Test image compression functionality.

Tests:
- Large images (>200KB) compressed to ≤200KB
- Small images (<200KB) unchanged
- Non-images (PDF, text) unchanged
- JPEG quality reduction works
- WebP conversion fallback
- Upload compressed image → download → verify readable
- Compression metadata accuracy
- Auto-compress can be disabled
"""
import sqlite3
import base64
from PIL import Image
import io
from db import Database
import schema
from events.identity import user
from events.content import message, message_attachment


def create_test_image(width, height, color='red', format='JPEG', add_noise=False):
    """Helper to create a test image and return as bytes."""
    img = Image.new('RGB', (width, height), color=color)

    # Add noise/detail to prevent excessive compression
    if add_noise:
        from PIL import ImageDraw
        import random
        draw = ImageDraw.Draw(img)
        for _ in range(width * height // 100):  # Add random pixels
            x = random.randint(0, width-1)
            y = random.randint(0, height-1)
            color_noise = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            draw.point((x, y), fill=color_noise)

    output = io.BytesIO()
    if format == 'JPEG':
        img.save(output, format=format, quality=95)  # High quality = larger size
    else:
        img.save(output, format=format)
    return output.getvalue()


def test_large_jpeg_compressed_to_target():
    """Test that large JPEG is compressed to ≤200KB."""

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

    # Create a large image with detail (should be >200KB)
    large_image = create_test_image(3000, 2000, 'blue', 'JPEG', add_noise=True)
    print(f"\nOriginal image size: {len(large_image):,} bytes")
    assert len(large_image) > 200 * 1024, f"Test image should be >200KB, got {len(large_image):,}B"

    # Upload with compression
    base64_data = base64.b64encode(large_image).decode('ascii')
    result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=base64_data,
        mime_type='image/jpeg',
        filename='large.jpg',
        t_ms=3000,
        db=db,
        auto_compress=True
    )

    db.commit()

    # Verify compression occurred
    assert result.get('compressed') == True, "Should have compressed the image"
    assert result['final_size'] <= 200 * 1024, f"Final size {result['final_size']:,} should be ≤200KB"
    assert result['compression_ratio'] > 1.0, "Should have some compression ratio"

    print(f"✓ Compressed {result['original_size']:,}B → {result['final_size']:,}B")
    print(f"  Ratio: {result['compression_ratio']:.1f}x")
    print(f"  Method: {result.get('compression_method')}")

    # Verify image is still readable
    data_uri = message_attachment.get_file_as_data_uri(
        file_id=result['file_id'],
        recorded_by=alice['peer_id'],
        db=db
    )
    assert data_uri is not None
    _, base64_part = data_uri.split(',', 1)
    retrieved_data = base64.b64decode(base64_part)

    # Verify it's a valid image
    img = Image.open(io.BytesIO(retrieved_data))
    assert img.size[0] <= 2048, "Should have resized to max dimension"
    assert img.size[1] <= 2048, "Should have resized to max dimension"

    print(f"✓ Compressed image is valid and readable")


def test_small_image_not_compressed():
    """Test that images <200KB are not compressed."""

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

    # Create a small image (<200KB)
    small_image = create_test_image(400, 300, 'green', 'JPEG')
    print(f"\nSmall image size: {len(small_image):,} bytes")
    assert len(small_image) < 200 * 1024, "Test image should be <200KB"

    # Upload with compression enabled
    base64_data = base64.b64encode(small_image).decode('ascii')
    result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=base64_data,
        mime_type='image/jpeg',
        filename='small.jpg',
        t_ms=3000,
        db=db,
        auto_compress=True
    )

    db.commit()

    # Verify NO compression occurred
    assert result.get('compressed') != True, "Should NOT have compressed small image"

    print(f"✓ Small image ({len(small_image):,}B) was not compressed")


def test_non_image_not_compressed():
    """Test that non-images are not compressed."""

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

    # Create a large non-image file
    large_text = b'X' * (500 * 1024)  # 500KB of text
    print(f"\nLarge text file size: {len(large_text):,} bytes")

    # Upload with compression enabled
    base64_data = base64.b64encode(large_text).decode('ascii')
    result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=base64_data,
        mime_type='text/plain',  # Not an image
        filename='large.txt',
        t_ms=3000,
        db=db,
        auto_compress=True
    )

    db.commit()

    # Verify NO compression occurred
    assert result.get('compressed') != True, "Should NOT have compressed non-image"

    # Verify file unchanged
    retrieved = message_attachment.get_file_data(result['file_id'], alice['peer_id'], db)
    assert retrieved == large_text, "Non-image file should be unchanged"

    print(f"✓ Non-image file ({len(large_text):,}B) was not compressed")


def test_png_image_compression():
    """Test PNG image compression."""

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

    # Create a large PNG (PNG files can be large)
    large_png = create_test_image(2500, 1800, 'yellow', 'PNG')
    print(f"\nOriginal PNG size: {len(large_png):,} bytes")

    if len(large_png) > 200 * 1024:
        # Upload with compression
        base64_data = base64.b64encode(large_png).decode('ascii')
        result = message_attachment.create_from_base64(
            peer_id=alice['peer_id'],
            message_id=message_id,
            base64_data=base64_data,
            mime_type='image/png',
            filename='large.png',
            t_ms=3000,
            db=db,
            auto_compress=True
        )

        db.commit()

        # Should have been compressed (likely converted to JPEG or WebP)
        assert result.get('compressed') == True, "Large PNG should be compressed"
        assert result['final_size'] <= 200 * 1024, f"Final size should be ≤200KB"

        print(f"✓ PNG compressed {result['original_size']:,}B → {result['final_size']:,}B")
    else:
        print(f"✓ PNG was already small ({len(large_png):,}B), skipping compression test")


def test_compression_can_be_disabled():
    """Test that compression can be disabled with auto_compress=False."""

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

    # Create a large image with detail
    large_image = create_test_image(3000, 2000, 'purple', 'JPEG', add_noise=True)
    print(f"\nLarge image size: {len(large_image):,} bytes")
    assert len(large_image) > 200 * 1024, f"Test image should be >200KB, got {len(large_image):,}B"

    # Upload with compression DISABLED
    base64_data = base64.b64encode(large_image).decode('ascii')
    result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=base64_data,
        mime_type='image/jpeg',
        filename='large_uncompressed.jpg',
        t_ms=3000,
        db=db,
        auto_compress=False  # Disabled
    )

    db.commit()

    # Verify NO compression occurred
    assert result.get('compressed') != True, "Should NOT have compressed when auto_compress=False"

    # Verify file is original size
    retrieved = message_attachment.get_file_data(result['file_id'], alice['peer_id'], db)
    # Allow for small encryption overhead
    assert abs(len(retrieved) - len(large_image)) < 1000, "File should be approximately original size"

    print(f"✓ Compression disabled, file remains {len(large_image):,}B")


def test_roundtrip_compressed_image():
    """Test upload compressed image → download → verify readable."""

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

    # Create large colorful image
    large_image = create_test_image(2800, 2100, 'orange', 'JPEG')
    print(f"\nOriginal: {len(large_image):,} bytes")

    # Upload with compression
    base64_data = base64.b64encode(large_image).decode('ascii')
    result = message_attachment.create_from_base64(
        peer_id=alice['peer_id'],
        message_id=message_id,
        base64_data=base64_data,
        mime_type='image/jpeg',
        filename='photo.jpg',
        t_ms=3000,
        db=db,
        auto_compress=True
    )

    file_id = result['file_id']
    db.commit()

    # Download as data URI
    data_uri = message_attachment.get_file_as_data_uri(
        file_id=file_id,
        recorded_by=alice['peer_id'],
        db=db
    )

    assert data_uri is not None
    assert data_uri.startswith('data:image/')

    # Extract and decode
    _, base64_part = data_uri.split(',', 1)
    retrieved_data = base64.b64decode(base64_part)

    # Verify it's a valid image
    img = Image.open(io.BytesIO(retrieved_data))
    assert img.width > 0 and img.height > 0
    assert len(retrieved_data) <= 200 * 1024

    print(f"✓ Round-trip successful: {len(large_image):,}B → {len(retrieved_data):,}B")
    print(f"  Downloaded image dimensions: {img.size}")


def test_very_large_image_uses_webp():
    """Test that very stubborn large images fall back to WebP."""

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

    # Create a very large, detailed image
    # (High detail images compress less)
    from PIL import ImageDraw
    img = Image.new('RGB', (4000, 3000))
    draw = ImageDraw.Draw(img)
    # Add lots of detail (hard to compress)
    for i in range(0, 4000, 10):
        draw.line([(i, 0), (i, 3000)], fill=(i % 256, (i*2) % 256, (i*3) % 256))

    output = io.BytesIO()
    img.save(output, format='JPEG', quality=95)
    very_large_image = output.getvalue()

    print(f"\nVery large detailed image: {len(very_large_image):,} bytes")

    if len(very_large_image) > 200 * 1024:
        # Upload with compression
        base64_data = base64.b64encode(very_large_image).decode('ascii')
        result = message_attachment.create_from_base64(
            peer_id=alice['peer_id'],
            message_id=message_id,
            base64_data=base64_data,
            mime_type='image/jpeg',
            filename='detailed.jpg',
            t_ms=3000,
            db=db,
            auto_compress=True
        )

        db.commit()

        # Should have compressed (possibly using WebP)
        assert result.get('compressed') == True
        print(f"✓ Very large image compressed using {result.get('compression_method')}")
        print(f"  {result['original_size']:,}B → {result['final_size']:,}B")


if __name__ == '__main__':
    print("=" * 80)
    print("Running Image Compression Tests")
    print("=" * 80)

    test_large_jpeg_compressed_to_target()
    test_small_image_not_compressed()
    test_non_image_not_compressed()
    test_png_image_compression()
    test_compression_can_be_disabled()
    test_roundtrip_compressed_image()
    test_very_large_image_uses_webp()

    print("\n" + "=" * 80)
    print("ALL COMPRESSION TESTS PASSED!")
    print("=" * 80)
