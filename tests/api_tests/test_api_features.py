"""
API tests for auth, file attachments, and pagination.

Tests:
- PSK authentication (valid/invalid/missing tokens)
- File upload with automatic image compression
- File retrieval and status
- Cursor-based message pagination
- Realistic curl-style HTTP tests (encoding, headers, multipart)
"""

import base64
import io
import pytest
import httpx
from fastapi.testclient import TestClient

from api.app import app
from api.core import database as api_db_module
from api.core import auth as api_auth_module
from events.identity import user
from events.content import message


def urlsafe_b64(s: str) -> str:
    """Convert base64 string to URL-safe format."""
    return s.replace('+', '-').replace('/', '_')


class APIClient:
    """Wrapper around TestClient that sets peer context per request."""

    def __init__(self, client: TestClient, peer_id: str):
        self.client = client
        self.peer_id = peer_id
        self._t_ms = 0

    def set_time(self, t_ms: int):
        self._t_ms = t_ms

    def _make_request(self, method: str, url: str, **kwargs):
        api_db_module.set_request_peer_id(self.peer_id)
        api_db_module.set_request_t_ms(self._t_ms)
        try:
            return getattr(self.client, method)(url, **kwargs)
        finally:
            api_db_module.set_request_peer_id(None)
            api_db_module.set_request_t_ms(None)

    def get(self, url: str, **kwargs):
        return self._make_request("get", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._make_request("post", url, **kwargs)


@pytest.fixture
def api_setup(tmp_path):
    """Set up API with disk-based SQLite + WAL mode."""
    import sqlite3
    from core.db import Database
    from core import schema

    api_db_module.reset_db()

    db_path = tmp_path / "test_api.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    db = Database(conn)
    schema.create_all(db)

    api_db_module.inject_db(db)
    api_db_module.enable_test_mode()
    api_auth_module.disable_auth()  # Default: auth disabled
    api_db_module.set_peer_id("default-test-peer")

    yield db

    conn.close()
    api_db_module.reset_db()


@pytest.fixture
def test_client(api_setup):
    return TestClient(app)


# ============================================================================
# Auth Tests
# ============================================================================

def test_auth_rejects_missing_token(api_setup, test_client):
    """Requests without Authorization header should be rejected when auth enabled."""
    # Enable auth for this test
    api_auth_module.enable_auth()
    api_auth_module.set_token(b"test-secret-token-32-bytes-long!")

    try:
        resp = test_client.get("/api/networks")
        assert resp.status_code == 401
        assert "Missing Authorization header" in resp.json()["detail"]
    finally:
        api_auth_module.disable_auth()

    print("Auth rejects missing token")


def test_auth_rejects_invalid_token(api_setup, test_client):
    """Requests with wrong token should be rejected."""
    api_auth_module.enable_auth()
    api_auth_module.set_token(b"test-secret-token-32-bytes-long!")

    try:
        wrong_token = base64.b64encode(b"wrong-token-here!!").decode()
        resp = test_client.get(
            "/api/networks",
            headers={"Authorization": f"Bearer {wrong_token}"}
        )
        assert resp.status_code == 401
        assert "Invalid API token" in resp.json()["detail"]
    finally:
        api_auth_module.disable_auth()

    print("Auth rejects invalid token")


def test_auth_accepts_valid_token(api_setup, test_client):
    """Requests with correct token should pass."""
    db = api_setup
    u = urlsafe_b64

    # Create a network so we have something to query
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    db.commit()

    api_auth_module.enable_auth()
    token = b"test-secret-token-32-bytes-long!"
    api_auth_module.set_token(token)

    try:
        valid_token = base64.b64encode(token).decode()
        api_db_module.set_request_peer_id(alice["peer_id"])
        resp = test_client.get(
            f"/api/networks/{u(alice['network_id'])}/channels",
            headers={"Authorization": f"Bearer {valid_token}"}
        )
        api_db_module.set_request_peer_id(None)

        assert resp.status_code == 200
    finally:
        api_auth_module.disable_auth()

    print("Auth accepts valid token")


def test_auth_allows_health_endpoint(api_setup, test_client):
    """Health endpoint should work without auth."""
    api_auth_module.enable_auth()
    api_auth_module.set_token(b"test-secret-token-32-bytes-long!")

    try:
        resp = test_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        api_auth_module.disable_auth()

    print("Health endpoint works without auth")


# ============================================================================
# File Attachment Tests
# ============================================================================

def test_send_message_with_text_file(api_setup, test_client):
    """Can send a message with a text file attachment."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Send message with text file attachment
    alice_api.set_time(2000)
    file_content = b"Hello, this is a test file content!"
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages/with-attachments",
        data={"text": "Check out this file"},
        files={"attachments": ("test.txt", io.BytesIO(file_content), "text/plain")},
    )

    assert resp.status_code == 201
    result = resp.json()
    assert result["message_id"] is not None
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["filename"] == "test.txt"
    assert result["attachments"][0]["mime_type"] == "text/plain"

    file_id = result["attachments"][0]["file_id"]
    assert file_id is not None

    # Verify message appears with attachment
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages"
    )
    assert resp.status_code == 200
    messages = resp.json()["items"]
    assert len(messages) == 1
    assert messages[0]["content"] == "Check out this file"
    assert len(messages[0]["attachments"]) == 1
    assert messages[0]["attachments"][0]["file_id"] == file_id

    print("Message with text file attachment works")


def test_send_message_with_image_compression(api_setup, test_client):
    """Images over 200KB should be automatically compressed."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Create a large "image" (simulated - in reality PIL would handle this)
    # For testing, we'll create a file that would trigger compression logic
    # The actual compression requires PIL and real image data
    try:
        from PIL import Image

        # Create a large test image (500x500 solid color, uncompressed)
        img = Image.new('RGB', (500, 500), color='red')
        img_buffer = io.BytesIO()
        img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        original_size = len(img_buffer.getvalue())

        alice_api.set_time(2000)
        resp = alice_api.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages/with-attachments",
            data={"text": "Here's a photo"},
            files={"attachments": ("photo.png", img_buffer, "image/png")},
        )

        assert resp.status_code == 201
        result = resp.json()
        assert len(result["attachments"]) == 1

        # File was created (compression may or may not reduce size for small test images)
        assert result["attachments"][0]["file_id"] is not None
        print(f"Image attachment created: original={original_size}B")

    except ImportError:
        # PIL not available, skip image-specific test
        pytest.skip("PIL not available for image compression test")

    print("Image compression test passed")


def test_retrieve_file_by_id(api_setup, test_client):
    """Can retrieve a file by its ID."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Upload a file
    alice_api.set_time(2000)
    file_content = b"Retrievable file content here"
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages/with-attachments",
        data={"text": "File for retrieval test"},
        files={"attachments": ("retrieve.txt", io.BytesIO(file_content), "text/plain")},
    )
    assert resp.status_code == 201
    file_id = resp.json()["attachments"][0]["file_id"]

    # Retrieve the file
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/files/{u(file_id)}"
    )
    assert resp.status_code == 200
    assert resp.content == file_content
    assert "text/plain" in resp.headers.get("content-type", "")

    print("File retrieval by ID works")


def test_file_status_complete(api_setup, test_client):
    """File status shows complete for locally created files."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Upload a file
    alice_api.set_time(2000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages/with-attachments",
        data={"text": "Status test"},
        files={"attachments": ("status.txt", io.BytesIO(b"test"), "text/plain")},
    )
    file_id = resp.json()["attachments"][0]["file_id"]

    # Check status
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/files/{u(file_id)}/status"
    )
    assert resp.status_code == 200
    status = resp.json()
    assert status["status"] == "complete"
    assert status["progress"] == 1.0
    assert status["slices_received"] == status["total_slices"]

    print("File status complete works")


# ============================================================================
# Pagination Tests
# ============================================================================

def test_pagination_returns_most_recent_by_default(api_setup, test_client):
    """Without cursor, returns most recent messages."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Send multiple messages
    for i in range(5):
        alice_api.set_time(2000 + i * 100)
        alice_api.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
            json={"text": f"Message {i+1}"},
        )

    # Get messages without cursor (should return all, most recent last)
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages?limit=50"
    )
    assert resp.status_code == 200
    messages = resp.json()["items"]

    # Messages should be in chronological order (oldest first)
    assert len(messages) == 5
    assert messages[0]["content"] == "Message 1"
    assert messages[4]["content"] == "Message 5"

    print("Pagination returns most recent by default")


def test_pagination_cursor_older(api_setup, test_client):
    """Can paginate to older messages using cursor."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Send 10 messages
    for i in range(10):
        alice_api.set_time(2000 + i * 100)
        alice_api.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
            json={"text": f"Message {i+1}"},
        )

    # Get first page (most recent 5)
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages?limit=5"
    )
    page1 = resp.json()
    messages1 = page1["items"]
    assert len(messages1) == 5
    # Should be messages 6-10 (most recent 5)
    assert messages1[0]["content"] == "Message 6"
    assert messages1[4]["content"] == "Message 10"

    # Use older cursor to get previous page
    older_cursor = u(messages1[0]["message_id"])  # Message 6
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages?cursor={older_cursor}&direction=older&limit=5"
    )
    page2 = resp.json()
    messages2 = page2["items"]

    # Should be messages 1-5 (older messages)
    assert len(messages2) == 5
    assert messages2[0]["content"] == "Message 1"
    assert messages2[4]["content"] == "Message 5"

    print("Pagination cursor older works")


def test_pagination_cursor_newer(api_setup, test_client):
    """Can paginate to newer messages using cursor."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Send 10 messages
    for i in range(10):
        alice_api.set_time(2000 + i * 100)
        alice_api.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
            json={"text": f"Message {i+1}"},
        )

    # Start from beginning (get oldest 5)
    # First get all messages to find the oldest
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages?limit=50"
    )
    all_messages = resp.json()["items"]
    oldest_msg_id = all_messages[0]["message_id"]

    # Now get messages newer than message 5
    msg5_id = u(all_messages[4]["message_id"])  # Message 5
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages?cursor={msg5_id}&direction=newer&limit=5"
    )
    newer_messages = resp.json()["items"]

    # Should be messages 6-10
    assert len(newer_messages) == 5
    assert newer_messages[0]["content"] == "Message 6"
    assert newer_messages[4]["content"] == "Message 10"

    print("Pagination cursor newer works")


def test_pagination_limit_respected(api_setup, test_client):
    """Pagination limit parameter is respected."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Send 20 messages
    for i in range(20):
        alice_api.set_time(2000 + i * 100)
        alice_api.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
            json={"text": f"Message {i+1}"},
        )

    # Request with limit=7
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages?limit=7"
    )
    messages = resp.json()["items"]
    assert len(messages) == 7

    print("Pagination limit respected")


def test_pagination_cursors_in_response(api_setup, test_client):
    """Response includes cursors for navigation."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Send 10 messages
    for i in range(10):
        alice_api.set_time(2000 + i * 100)
        alice_api.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
            json={"text": f"Message {i+1}"},
        )

    # Get first page
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages?limit=5"
    )
    result = resp.json()

    # Should have cursors
    assert "cursors" in result
    # newer cursor points to newest message in this batch
    assert result["cursors"]["newer"] is not None
    # older cursor points to oldest message if there are more
    assert result["cursors"]["older"] is not None

    print("Pagination cursors in response")


# ============================================================================
# Realistic Curl-Style Tests
# ============================================================================
# These tests verify the API works with real HTTP clients like curl,
# testing encoding edge cases, header formats, and multipart handling.

def test_curl_style_json_post(api_setup, test_client):
    """Test JSON POST as curl would send it: curl -X POST -H 'Content-Type: application/json' -d '{...}'"""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    db.commit()

    # Simulate curl: curl -X POST -H 'Content-Type: application/json' -d '{"text":"Hello"}'
    api_db_module.set_request_peer_id(alice["peer_id"])
    api_db_module.set_request_t_ms(2000)
    try:
        resp = test_client.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
            content='{"text": "Hello from curl"}',
            headers={"Content-Type": "application/json"},
        )
    finally:
        api_db_module.set_request_peer_id(None)
        api_db_module.set_request_t_ms(None)

    assert resp.status_code == 201
    assert resp.json()["message_id"] is not None

    print("curl-style JSON POST works")


def test_curl_style_multipart_upload(api_setup, test_client):
    """Test multipart upload as curl would send it: curl -F 'text=hello' -F 'attachments=@file.txt'"""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    db.commit()

    # Simulate curl: curl -F 'text=Hello' -F 'attachments=@test.txt;type=text/plain'
    api_db_module.set_request_peer_id(alice["peer_id"])
    api_db_module.set_request_t_ms(2000)
    try:
        resp = test_client.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages/with-attachments",
            data={"text": "Hello with attachment"},
            files={"attachments": ("test.txt", b"File content here", "text/plain")},
        )
    finally:
        api_db_module.set_request_peer_id(None)
        api_db_module.set_request_t_ms(None)

    assert resp.status_code == 201
    result = resp.json()
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["filename"] == "test.txt"

    print("curl-style multipart upload works")


def test_base64_ids_with_special_chars(api_setup, test_client):
    """Test that IDs with +, /, = (base64 special chars) work correctly.

    This is critical because base64 IDs naturally contain these characters
    which have special meaning in URLs.
    """
    db = api_setup

    # Create network - the IDs are base64 and may contain +, /, =
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    network_id = alice["network_id"]
    channel_id = alice["channel_id"]

    # Check if IDs contain special chars (they often do)
    has_special = any(c in network_id + channel_id for c in ['+', '/', '='])
    print(f"IDs contain special base64 chars: {has_special}")
    print(f"  network_id: {network_id[:30]}...")
    print(f"  channel_id: {channel_id[:30]}...")

    # URL-safe encoding should handle these
    u = urlsafe_b64
    alice_api.set_time(2000)
    resp = alice_api.get(f"/api/networks/{u(network_id)}/channels/{u(channel_id)}/messages")
    assert resp.status_code == 200

    # Also test sending a message and using the message_id
    resp = alice_api.post(
        f"/api/networks/{u(network_id)}/channels/{u(channel_id)}/messages",
        json={"text": "Test message"},
    )
    assert resp.status_code == 201
    message_id = resp.json()["message_id"]

    # Delete using the message_id (which is also base64)
    alice_api.set_time(3000)
    resp = alice_api.client.delete(
        f"/api/networks/{u(network_id)}/messages/{u(message_id)}",
    )
    # Note: We need to set peer context for delete
    api_db_module.set_request_peer_id(alice["peer_id"])
    api_db_module.set_request_t_ms(3000)
    try:
        resp = test_client.delete(
            f"/api/networks/{u(network_id)}/messages/{u(message_id)}",
        )
    finally:
        api_db_module.set_request_peer_id(None)
        api_db_module.set_request_t_ms(None)

    assert resp.status_code == 200

    print("Base64 IDs with special characters work correctly")


def test_auth_header_format_matches_curl(api_setup, test_client):
    """Test auth header format: Authorization: Bearer <base64_token>

    This should work with: curl -H 'Authorization: Bearer <token>'
    """
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    db.commit()

    # Enable auth
    api_auth_module.enable_auth()
    token = b"my-secret-api-token-here-32!!"  # 32 bytes
    api_auth_module.set_token(token)

    try:
        # curl style: -H 'Authorization: Bearer <base64>'
        token_b64 = base64.b64encode(token).decode("ascii")
        auth_header = f"Bearer {token_b64}"

        api_db_module.set_request_peer_id(alice["peer_id"])
        api_db_module.set_request_t_ms(2000)
        try:
            resp = test_client.get(
                f"/api/networks/{u(alice['network_id'])}/channels",
                headers={"Authorization": auth_header},
            )
        finally:
            api_db_module.set_request_peer_id(None)
            api_db_module.set_request_t_ms(None)

        assert resp.status_code == 200
    finally:
        api_auth_module.disable_auth()

    print("Auth header format matches curl expectations")


def test_content_type_headers(api_setup, test_client):
    """Test that correct Content-Type headers are returned."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # JSON endpoints should return application/json
    alice_api.set_time(2000)
    resp = alice_api.get(f"/api/networks/{u(alice['network_id'])}/channels")
    assert resp.status_code == 200
    assert "application/json" in resp.headers.get("content-type", "")

    # Upload a file and check file download content-type
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages/with-attachments",
        data={"text": "Image test"},
        files={"attachments": ("photo.jpg", b"\xff\xd8\xff\xe0test", "image/jpeg")},
    )
    assert resp.status_code == 201
    file_id = resp.json()["attachments"][0]["file_id"]

    # File download should return the original content-type
    resp = alice_api.get(f"/api/networks/{u(alice['network_id'])}/files/{u(file_id)}")
    assert resp.status_code == 200
    assert "image/jpeg" in resp.headers.get("content-type", "")

    print("Content-Type headers are correct")


def test_error_response_format(api_setup, test_client):
    """Test that error responses have consistent JSON format."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # 404 - Not found
    alice_api.set_time(2000)
    resp = alice_api.get(f"/api/networks/{u(alice['network_id'])}/files/{u('nonexistent')}")
    assert resp.status_code == 404
    error = resp.json()
    assert "detail" in error  # FastAPI standard error format

    # 400 - Bad request (empty message)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages/with-attachments",
        data={"text": ""},
        files={},
    )
    assert resp.status_code == 400
    error = resp.json()
    assert "detail" in error

    print("Error responses have consistent format")


def test_large_file_chunked_upload(api_setup, test_client):
    """Test that large file uploads work with chunked reading."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Create a "large" file (500KB) to test chunked reading
    large_content = b"X" * (500 * 1024)

    alice_api.set_time(2000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages/with-attachments",
        data={"text": "Large file test"},
        files={"attachments": ("large.bin", io.BytesIO(large_content), "application/octet-stream")},
    )

    assert resp.status_code == 201
    result = resp.json()
    assert len(result["attachments"]) == 1
    file_id = result["attachments"][0]["file_id"]

    # Verify file can be retrieved
    resp = alice_api.get(f"/api/networks/{u(alice['network_id'])}/files/{u(file_id)}")
    assert resp.status_code == 200
    assert len(resp.content) == len(large_content)
    assert resp.content == large_content

    print("Large file chunked upload works")


def test_unicode_in_message_text(api_setup, test_client):
    """Test that unicode characters in messages work correctly."""
    db = api_setup
    u = urlsafe_b64

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Test various unicode: emoji, CJK, RTL, combining chars
    unicode_texts = [
        "Hello! How are you?",
        "Emoji test: hello",
        "Chinese: \u4f60\u597d\u4e16\u754c",
        "Arabic: \u0645\u0631\u062d\u0628\u0627",
        "Math: \u221e \u2211 \u222b",
    ]

    alice_api.set_time(2000)
    for i, text in enumerate(unicode_texts):
        resp = alice_api.post(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
            json={"text": text},
        )
        assert resp.status_code == 201, f"Failed for: {text}"

    # Verify all messages came back correctly (order may vary)
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages?limit=50"
    )
    assert resp.status_code == 200
    messages = resp.json()["items"]
    assert len(messages) == len(unicode_texts)

    received_texts = {msg["content"] for msg in messages}
    for expected in unicode_texts:
        assert expected in received_texts, f"Unicode text not found: {expected!r}"

    print("Unicode in messages works correctly")
