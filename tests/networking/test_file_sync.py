"""File attachment sync over real UDP.

Tests file attachments of various sizes syncing between
separate databases over real UDP sockets.

SKIPPED: These tests use the old queues.set_transport_callback() system
which is incompatible with the new core/transport.py system. Real networking
tests need to be refactored to use multi-instance (separate processes).
"""

import pytest

# Skip all tests in this module - old transport callback system incompatible with new transport.py
pytestmark = pytest.mark.skip(reason="Uses old transport callback system; needs multi-instance refactor")
import os
from events.content import message, message_attachment
from tests.networking.conftest import (
    create_network_and_join, tick_until, now_ms
)


def create_test_image(width=100, height=100) -> bytes:
    """Create a simple test PNG image."""
    from PIL import Image
    import io

    img = Image.new('RGB', (width, height), color='red')
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return buffer.getvalue()


def create_random_bytes(size: int) -> bytes:
    """Create random bytes of specified size."""
    return os.urandom(size)


class TestImageAttachmentSync:
    """Test image attachments syncing over real UDP."""

    def test_small_image_sync(self, two_clients):
        """Test syncing a small image attachment."""
        alice = two_clients["alice"]
        bob = two_clients["bob"]

        # Set up network
        channel_id = create_network_and_join(two_clients)

        # Initial ticks to establish connections
        tick_until(two_clients, lambda: False, max_ticks=30, tick_interval=0.05)

        # Alice creates a message with attachment
        t_ms = now_ms()
        msg_result = message.create(
            content="Check out this image!",
            channel_id=channel_id,
            peer_id=alice.peer_id,
            t_ms=t_ms,
            db=alice.db
        )
        alice.db.commit()
        msg_id = msg_result['id']

        # Alice attaches a small image
        t_ms = now_ms()
        image_data = create_test_image(50, 50)  # Small 50x50 PNG
        attach_result = message_attachment.create(
            peer_id=alice.peer_id,
            message_id=msg_id,
            file_data=image_data,
            filename="test.png",
            mime_type="image/png",
            t_ms=t_ms,
            db=alice.db
        )
        alice.db.commit()
        file_id = attach_result['file_id']

        # Wait for Bob to receive the message and attachment
        def bob_has_attachment():
            bob_msgs = message.list(channel_id, bob.peer_id, bob.db)
            for msg in bob_msgs:
                if msg.get('attachments'):
                    for att in msg['attachments']:
                        if att.get('file_id') == file_id:
                            return True
            return False

        ticks = tick_until(two_clients, bob_has_attachment, max_ticks=150)
        assert ticks is not None, "Bob didn't receive the image attachment"

        # Verify attachment details
        bob_msgs = message.list(channel_id, bob.peer_id, bob.db)
        bob_attachment = None
        for msg in bob_msgs:
            for att in msg.get('attachments', []):
                if att.get('file_id') == file_id:
                    bob_attachment = att
                    break

        assert bob_attachment is not None
        assert bob_attachment['filename'] == "test.png"
        assert bob_attachment['mime_type'] == "image/png"
        assert bob_attachment['blob_bytes'] == len(image_data)


class TestFileSizeProgression:
    """Test files of increasing sizes syncing over real UDP."""

    def test_1kb_file_sync(self, two_clients):
        """Test syncing a 1KB file."""
        self._test_file_sync(two_clients, 1024, "1kb_file.bin")

    def test_10kb_file_sync(self, two_clients):
        """Test syncing a 10KB file."""
        self._test_file_sync(two_clients, 10 * 1024, "10kb_file.bin")

    def test_50kb_file_sync(self, two_clients):
        """Test syncing a 50KB file."""
        self._test_file_sync(two_clients, 50 * 1024, "50kb_file.bin")

    def test_100kb_file_sync(self, two_clients):
        """Test syncing a 100KB file."""
        self._test_file_sync(two_clients, 100 * 1024, "100kb_file.bin")

    @pytest.mark.slow
    def test_500kb_file_sync(self, two_clients):
        """Test syncing a 500KB file (slower test)."""
        self._test_file_sync(two_clients, 500 * 1024, "500kb_file.bin", max_ticks=300)

    @pytest.mark.slow
    def test_1mb_file_sync(self, two_clients):
        """Test syncing a 1MB file (slower test)."""
        self._test_file_sync(two_clients, 1024 * 1024, "1mb_file.bin", max_ticks=500)

    def _test_file_sync(self, two_clients, file_size: int, filename: str, max_ticks: int = 200):
        """Helper to test file sync of given size."""
        alice = two_clients["alice"]
        bob = two_clients["bob"]

        # Set up network
        channel_id = create_network_and_join(two_clients)

        # Initial ticks to establish connections
        tick_until(two_clients, lambda: False, max_ticks=30, tick_interval=0.05)

        # Alice creates a message
        t_ms = now_ms()
        msg_result = message.create(
            content=f"File: {filename}",
            channel_id=channel_id,
            peer_id=alice.peer_id,
            t_ms=t_ms,
            db=alice.db
        )
        alice.db.commit()
        msg_id = msg_result['id']

        # Alice attaches the file
        t_ms = now_ms()
        file_data = create_random_bytes(file_size)
        attach_result = message_attachment.create(
            peer_id=alice.peer_id,
            message_id=msg_id,
            file_data=file_data,
            filename=filename,
            mime_type="application/octet-stream",
            t_ms=t_ms,
            db=alice.db
        )
        alice.db.commit()
        file_id = attach_result['file_id']
        slice_count = attach_result['slice_count']

        # Wait for Bob to receive the attachment
        def bob_has_attachment():
            bob_msgs = message.list(channel_id, bob.peer_id, bob.db)
            for msg in bob_msgs:
                for att in msg.get('attachments', []):
                    if att.get('file_id') == file_id:
                        return True
            return False

        ticks = tick_until(two_clients, bob_has_attachment, max_ticks=max_ticks)
        assert ticks is not None, f"Bob didn't receive {filename} ({file_size} bytes, {slice_count} slices)"

        # Verify
        bob_msgs = message.list(channel_id, bob.peer_id, bob.db)
        bob_attachment = None
        for msg in bob_msgs:
            for att in msg.get('attachments', []):
                if att.get('file_id') == file_id:
                    bob_attachment = att
                    break

        assert bob_attachment is not None
        assert bob_attachment['blob_bytes'] == file_size
        assert bob_attachment['filename'] == filename


class TestBidirectionalFileSync:
    """Test files syncing in both directions."""

    def test_alice_and_bob_exchange_files(self, two_clients):
        """Test both Alice and Bob sending files to each other."""
        alice = two_clients["alice"]
        bob = two_clients["bob"]

        # Set up network
        channel_id = create_network_and_join(two_clients)

        # Initial ticks
        tick_until(two_clients, lambda: False, max_ticks=30, tick_interval=0.05)

        # Alice sends a file
        t_ms = now_ms()
        alice_msg = message.create(
            content="Alice's file",
            channel_id=channel_id,
            peer_id=alice.peer_id,
            t_ms=t_ms,
            db=alice.db
        )
        alice.db.commit()

        t_ms = now_ms()
        alice_file = create_random_bytes(5000)
        alice_attach = message_attachment.create(
            peer_id=alice.peer_id,
            message_id=alice_msg['id'],
            file_data=alice_file,
            filename="from_alice.bin",
            mime_type="application/octet-stream",
            t_ms=t_ms,
            db=alice.db
        )
        alice.db.commit()

        # Bob sends a file
        t_ms = now_ms()
        bob_msg = message.create(
            content="Bob's file",
            channel_id=channel_id,
            peer_id=bob.peer_id,
            t_ms=t_ms,
            db=bob.db
        )
        bob.db.commit()

        t_ms = now_ms()
        bob_file = create_random_bytes(7000)
        bob_attach = message_attachment.create(
            peer_id=bob.peer_id,
            message_id=bob_msg['id'],
            file_data=bob_file,
            filename="from_bob.bin",
            mime_type="application/octet-stream",
            t_ms=t_ms,
            db=bob.db
        )
        bob.db.commit()

        # Wait for both to sync
        def both_have_each_others_files():
            alice_msgs = message.list(channel_id, alice.peer_id, alice.db)
            bob_msgs = message.list(channel_id, bob.peer_id, bob.db)

            alice_has_bob_file = any(
                att.get('file_id') == bob_attach['file_id']
                for msg in alice_msgs
                for att in msg.get('attachments', [])
            )
            bob_has_alice_file = any(
                att.get('file_id') == alice_attach['file_id']
                for msg in bob_msgs
                for att in msg.get('attachments', [])
            )

            return alice_has_bob_file and bob_has_alice_file

        ticks = tick_until(two_clients, both_have_each_others_files, max_ticks=200)
        assert ticks is not None, "Files didn't sync in both directions"
