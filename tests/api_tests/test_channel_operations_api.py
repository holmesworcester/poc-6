"""
API tests for channel operations: listing and creation.

Tests that the API correctly handles:
- Listing channels in a network
- Creating new channels (admin only)
- Getting individual channel details
"""

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.core import database as api_db_module
from api.core import auth as api_auth_module
from events.identity import user, invite, peer
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


def test_list_channels_shows_main_channel(api_setup, test_client):
    """Network creation includes a main channel that appears in the list."""
    db = api_setup
    u = urlsafe_b64

    # Alice creates network
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # List channels
    alice_api.set_time(2000)
    resp = alice_api.get(f"/api/networks/{u(alice['network_id'])}/channels")
    assert resp.status_code == 200

    channels = resp.json()["items"]
    assert len(channels) == 1, "Should have one main channel"

    main_channel = channels[0]
    assert main_channel["channel_id"] == alice["channel_id"]
    assert main_channel["name"] == "general"

    print("✓ Main channel appears in channel list")


def test_admin_can_create_channel(api_setup, test_client):
    """Admin (network creator) can create additional channels."""
    db = api_setup
    u = urlsafe_b64

    # Alice creates network (she's admin)
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Alice creates a new channel
    alice_api.set_time(2000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels",
        json={"name": "random", "disappearing_time_ms": 0},
    )
    assert resp.status_code == 201
    new_channel_id = resp.json()["channel_id"]
    assert new_channel_id is not None

    # Verify channel appears in list
    resp = alice_api.get(f"/api/networks/{u(alice['network_id'])}/channels")
    assert resp.status_code == 200

    channels = resp.json()["items"]
    assert len(channels) == 2, "Should have main channel + new channel"

    channel_names = [ch["name"] for ch in channels]
    assert "general" in channel_names
    assert "random" in channel_names

    print("✓ Admin can create channels via API")


def test_non_admin_cannot_create_channel(api_setup, test_client):
    """Non-admin user cannot create channels."""
    db = api_setup
    u = urlsafe_b64

    # Alice creates network
    alice = user.new_network(name="Alice", t_ms=1000, db=db)

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

    # Sync so Bob has full network state
    for i in range(50):
        tick.tick(t_ms=3000 + i * tick_helper.TICK_INTERVAL_MS, db=db)

    # Bob tries to create a channel (should fail - not admin)
    bob_api.set_time(5000)
    resp = bob_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels",
        json={"name": "bob-channel"},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()

    print("✓ Non-admin cannot create channels (403)")


def test_get_channel_details(api_setup, test_client):
    """Can get details of a specific channel."""
    db = api_setup
    u = urlsafe_b64

    # Alice creates network
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Get main channel details
    alice_api.set_time(2000)
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(alice['channel_id'])}"
    )
    assert resp.status_code == 200

    channel = resp.json()
    assert channel["channel_id"] == alice["channel_id"]
    assert channel["name"] == "general"
    assert "group_id" in channel

    print("✓ Get channel details works via API")


def test_new_channel_visible_to_other_users(api_setup, test_client):
    """Channel created by admin is visible to other users after sync."""
    db = api_setup
    u = urlsafe_b64

    # Alice creates network
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])

    # Bob joins
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

    # Alice creates a new channel
    alice_api.set_time(5000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels",
        json={"name": "announcements"},
    )
    assert resp.status_code == 201
    db.commit()

    # Sync so Bob sees the new channel
    def bob_sees_announcements():
        resp = bob_api.get(f"/api/networks/{u(alice['network_id'])}/channels")
        assert resp.status_code == 200
        channels = resp.json()["items"]
        channel_names = [ch["name"] for ch in channels]
        assert "announcements" in channel_names, f"Bob only sees: {channel_names}"

    tick_helper.assert_eventually(bob_sees_announcements, db=db, start_t_ms=6000)

    print("✓ New channel visible to other users after sync")


def test_can_send_message_to_new_channel(api_setup, test_client):
    """Users can send messages to channels created by admin."""
    db = api_setup
    u = urlsafe_b64

    # Alice creates network
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_api = APIClient(test_client, alice["peer_id"])
    db.commit()

    # Alice creates a new channel
    alice_api.set_time(2000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels",
        json={"name": "random"},
    )
    assert resp.status_code == 201
    new_channel_id = resp.json()["channel_id"]
    db.commit()

    # Alice sends a message to the new channel
    alice_api.set_time(3000)
    resp = alice_api.post(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(new_channel_id)}/messages",
        json={"text": "Hello random channel!"},
    )
    assert resp.status_code == 201

    # Verify message is in the channel
    resp = alice_api.get(
        f"/api/networks/{u(alice['network_id'])}/channels/{u(new_channel_id)}/messages"
    )
    assert resp.status_code == 200
    messages = resp.json()["items"]
    assert len(messages) == 1
    assert messages[0]["content"] == "Hello random channel!"

    print("✓ Can send messages to newly created channels")
