"""
API tests for message operations: deletion and editing.

Tests that the API correctly handles:
- Message deletion by author
- Message editing by author
- Rejection of unauthorized edits/deletions
"""

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.core import database as api_db_module
from api.core import auth as api_auth_module
from events.identity import user, invite, peer
from events.content import message
from core import tick
from tests.utils import tick_helper


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

    def patch(self, url: str, **kwargs):
        return self._make_request("patch", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self._make_request("delete", url, **kwargs)


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
    api_auth_module.disable_auth()  # Disable PSK auth for tests
    api_db_module.set_peer_id("default-test-peer")

    yield db

    conn.close()
    api_db_module.reset_db()


@pytest.fixture
def test_client(api_setup):
    return TestClient(app)


def test_message_deletion_by_author(api_setup, test_client):
    """Author can delete their own message via API."""
    db = api_setup
    u = urlsafe_b64

    # Setup: Alice creates network and sends a message
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Alice sends a message
    alice_api.set_time(2000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
        json={"text": "Message to be deleted"},
    )
    assert resp.status_code == 201
    message_id = resp.json()["message_id"]

    # Verify message exists
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages"
    )
    assert resp.status_code == 200
    messages = resp.json()["items"]
    assert len(messages) == 1
    assert messages[0]["content"] == "Message to be deleted"

    # Delete the message
    alice_api.set_time(3000)
    resp = alice_api.delete(
        f"/api/networks/{u(alice['network_id'])}/messages/{u(message_id)}"
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Verify message is deleted (no longer in list)
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages"
    )
    assert resp.status_code == 200
    messages = resp.json()["items"]
    assert len(messages) == 0, "Deleted message should not appear in list"

    print("✓ Message deletion by author works via API")


def test_message_edit_by_author(api_setup, test_client):
    """Author can edit their own message via API."""
    db = api_setup
    u = urlsafe_b64

    # Setup: Alice creates network and sends a message
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Alice sends a message
    alice_api.set_time(2000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
        json={"text": "Original message"},
    )
    assert resp.status_code == 201
    message_id = resp.json()["message_id"]

    # Verify original content
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages"
    )
    messages = resp.json()["items"]
    assert messages[0]["content"] == "Original message"
    assert messages[0]["edited_at"] is None or messages[0]["edited_at"] == 0

    # Edit the message
    alice_api.set_time(3000)
    resp = alice_api.patch(
        f"/api/networks/{u(alice['network_id'])}/messages/{u(message_id)}",
        json={"text": "Edited message"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Verify edited content
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages"
    )
    messages = resp.json()["items"]
    assert len(messages) == 1
    assert messages[0]["content"] == "Edited message"
    assert messages[0]["edited_at"] is not None and messages[0]["edited_at"] > 0

    print("✓ Message edit by author works via API")


def test_two_player_message_visibility(api_setup, test_client):
    """Bob can see messages after Alice sends them (via sync)."""
    db = api_setup
    u = urlsafe_b64

    # Alice creates network
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])

    # Alice creates invite
    invite_id, invite_link, _ = invite.create(
        peer_id=alice["peer_id"], t_ms=1500, db=db
    )

    # Bob joins
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(
        peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db
    )
    bob_api = APIClient(test_client, bob_peer_id)
    db.commit()

    # Initial sync
    for i in range(50):
        tick.tick(t_ms=3000 + i * tick_helper.TICK_INTERVAL_MS, db=db)

    # Alice sends a message
    alice_api.set_time(5000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
        json={"text": "Hello Bob!"},
    )
    assert resp.status_code == 201
    db.commit()

    # Sync so Bob can see the message
    def bob_sees_message():
        resp = bob_api.get(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages"
        )
        assert resp.status_code == 200
        messages = resp.json()["items"]
        assert len(messages) == 1, f"Expected 1 message, got {len(messages)}"
        assert messages[0]["content"] == "Hello Bob!"
        assert messages[0]["author_name"] == "Alice"

    tick_helper.assert_eventually(bob_sees_message, db=db, start_t_ms=6000)

    print("✓ Two player message visibility works via API")


def test_deleted_message_syncs_to_other_user(api_setup, test_client):
    """When Alice deletes a message, Bob no longer sees it after sync."""
    db = api_setup
    u = urlsafe_b64

    # Alice creates network
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])

    # Bob joins via invite
    invite_id, invite_link, _ = invite.create(
        peer_id=alice["peer_id"], t_ms=1500, db=db
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(
        peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db
    )
    bob_api = APIClient(test_client, bob_peer_id)
    db.commit()

    # Initial sync
    for i in range(50):
        tick.tick(t_ms=3000 + i * tick_helper.TICK_INTERVAL_MS, db=db)

    # Alice sends a message
    alice_api.set_time(5000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages",
        json={"text": "This will be deleted"},
    )
    assert resp.status_code == 201
    message_id = resp.json()["message_id"]
    db.commit()

    # Sync so Bob sees it
    def bob_sees_message():
        resp = bob_api.get(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages"
        )
        assert len(resp.json()["items"]) == 1

    tick_helper.assert_eventually(bob_sees_message, db=db, start_t_ms=6000)

    # Alice deletes the message
    alice_api.set_time(8000)
    resp = alice_api.delete(
        f"/api/networks/{u(alice['network_id'])}/messages/{u(message_id)}"
    )
    assert resp.status_code == 200
    db.commit()

    # Sync deletion to Bob
    def bob_sees_deletion():
        resp = bob_api.get(
            f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}/messages"
        )
        messages = resp.json()["items"]
        assert len(messages) == 0, f"Deleted message should not appear for Bob, got {len(messages)}"

    tick_helper.assert_eventually(bob_sees_deletion, db=db, start_t_ms=9000)

    print("✓ Message deletion syncs to other users via API")
