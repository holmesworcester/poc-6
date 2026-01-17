"""
API test: Three players with message exchange via HTTP API.

This test mirrors test_three_player_messaging but uses the HTTP API
instead of calling event modules directly.

Alice creates a network. Bob joins Alice's network via invite.
Charlie creates his own separate network.

Tests that the API correctly:
- Creates networks and invites
- Lists channels and messages
- Enforces network isolation (Charlie can't see Alice/Bob's messages)
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
    """Convert base64 string to URL-safe format.

    Replaces + with - and / with _ to make base64 IDs URL-safe.
    This is the standard URL-safe base64 encoding (RFC 4648).
    """
    return s.replace('+', '-').replace('/', '_')


class APIClient:
    """Wrapper around TestClient that sets peer context per request."""

    def __init__(self, client: TestClient, peer_id: str):
        self.client = client
        self.peer_id = peer_id
        self._t_ms = 0

    def set_time(self, t_ms: int):
        """Set the timestamp for subsequent requests."""
        self._t_ms = t_ms

    def _make_request(self, method: str, url: str, **kwargs):
        """Make a request with peer context set."""
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

    def delete(self, url: str, **kwargs):
        return self._make_request("delete", url, **kwargs)


@pytest.fixture
def api_setup(tmp_path):
    """Set up API with disk-based SQLite + WAL mode (matches production)."""
    import sqlite3
    from core.db import Database
    from core import schema

    # Reset any previous state
    api_db_module.reset_db()

    # Create disk-based database with WAL mode (like production)
    db_path = tmp_path / "test_api.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB
    conn.execute("PRAGMA temp_store=MEMORY")
    db = Database(conn)
    schema.create_all(db)

    # Inject the database into the API module
    api_db_module.inject_db(db)
    api_db_module.enable_test_mode()
    api_auth_module.disable_auth()  # Disable PSK auth for tests
    # Set a default peer_id (will be overridden per-request in test mode)
    api_db_module.set_peer_id("default-test-peer")

    yield db

    # Cleanup
    conn.close()
    api_db_module.reset_db()


@pytest.fixture
def test_client(api_setup):
    """Create a FastAPI test client."""
    return TestClient(app)


def test_three_player_api(api_setup, test_client):
    """Three peers: Alice creates network, Bob joins, Charlie separate - via API."""
    db = api_setup

    print("\n=== Setup: Create networks and peers directly ===")

    # Alice creates a network (using event modules directly for setup)
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    alice_peer_id = alice["peer_id"]
    alice_network_id = alice["network_id"]
    alice_channel_id = alice["channel_id"]
    print(f"Alice created network: {alice_network_id[:20]}...")
    print(f"Alice's channel: {alice_channel_id[:20]}...")

    # Create Alice's API client
    alice_api = APIClient(test_client, alice_peer_id)

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice_peer_id,
        t_ms=1500,
        db=db,
    )
    print(f"Alice created invite: {invite_id[:20]}...")

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(
        peer_id=bob_peer_id,
        invite_link=invite_link,
        name="Bob",
        t_ms=2000,
        db=db,
    )
    print(f"Bob joined network: {bob['peer_id'][:20]}...")

    # Create Bob's API client
    bob_api = APIClient(test_client, bob_peer_id)

    # Charlie creates his own separate network
    charlie = user.new_network(name="Charlie", t_ms=3000, db=db)
    charlie_peer_id = charlie["peer_id"]
    charlie_network_id = charlie["network_id"]
    charlie_channel_id = charlie["channel_id"]
    print(f"Charlie created separate network: {charlie_network_id[:20]}...")

    # Create Charlie's API client
    charlie_api = APIClient(test_client, charlie_peer_id)

    db.commit()

    # Run sync so Alice and Bob can see each other's events
    print("\n=== Initial sync ===")
    num_rounds = 100
    for i in range(num_rounds):
        tick.tick(t_ms=4000 + i * tick_helper.TICK_INTERVAL_MS, db=db)
    print(f"Initial sync completed after {num_rounds} ticks")

    # === Test API: List networks ===
    print("\n=== API Test: List networks ===")

    alice_api.set_time(5000)
    resp = alice_api.get("/api/networks")
    assert resp.status_code == 200, f"Alice list networks failed: {resp.text}"
    alice_networks = resp.json()["items"]
    assert len(alice_networks) == 1, f"Alice should have 1 network, got {len(alice_networks)}"
    assert alice_networks[0]["network_id"] == alice_network_id
    print(f"Alice sees network: {alice_networks[0]['network_id'][:20]}...")

    bob_api.set_time(5000)
    resp = bob_api.get("/api/networks")
    assert resp.status_code == 200, f"Bob list networks failed: {resp.text}"
    bob_networks = resp.json()["items"]
    assert len(bob_networks) == 1, f"Bob should have 1 network, got {len(bob_networks)}"
    # Bob should see the same network as Alice (he joined her network)
    assert bob_networks[0]["network_id"] == alice_network_id
    print(f"Bob sees network: {bob_networks[0]['network_id'][:20]}...")

    charlie_api.set_time(5000)
    resp = charlie_api.get("/api/networks")
    assert resp.status_code == 200, f"Charlie list networks failed: {resp.text}"
    charlie_networks = resp.json()["items"]
    assert len(charlie_networks) == 1
    assert charlie_networks[0]["network_id"] == charlie_network_id
    print(f"Charlie sees network: {charlie_networks[0]['network_id'][:20]}...")

    # === Test API: List channels ===
    print("\n=== API Test: List channels ===")

    # Use urlsafe_b64 for IDs in URLs (converts + to - and / to _)
    u = urlsafe_b64

    resp = alice_api.get(f"/api/networks/{u(alice_network_id)}/channels")
    assert resp.status_code == 200, f"Alice list channels failed: {resp.text}"
    alice_channels = resp.json()["items"]
    assert len(alice_channels) == 1
    api_channel_id = alice_channels[0]["channel_id"]
    assert api_channel_id == alice_channel_id
    print(f"Alice sees channel: {api_channel_id[:20]}...")

    resp = bob_api.get(f"/api/networks/{u(alice_network_id)}/channels")
    assert resp.status_code == 200, f"Bob list channels failed: {resp.text}"
    bob_channels = resp.json()["items"]
    assert len(bob_channels) == 1
    assert bob_channels[0]["channel_id"] == alice_channel_id
    print(f"Bob sees channel: {bob_channels[0]['channel_id'][:20]}...")

    # Charlie should NOT see Alice's channels (different network)
    resp = charlie_api.get(f"/api/networks/{u(alice_network_id)}/channels")
    assert resp.status_code == 404, f"Charlie should get 404 for Alice's network: {resp.text}"
    print("Charlie correctly cannot see Alice's network (404)")

    # === Test API: Send messages ===
    print("\n=== API Test: Send messages ===")

    # Alice sends a message
    alice_api.set_time(6000)
    resp = alice_api.post(
        f"/api/networks/{u(alice_network_id)}/channels/{u(alice_channel_id)}/messages",
        json={"text": "Hello from Alice via API!"},
    )
    assert resp.status_code == 201, f"Alice send message failed: {resp.text}"
    alice_msg_id = resp.json()["message_id"]
    print(f"Alice sent message: {alice_msg_id[:20]}...")

    # Bob sends a message
    bob_api.set_time(6100)
    resp = bob_api.post(
        f"/api/networks/{u(alice_network_id)}/channels/{u(alice_channel_id)}/messages",
        json={"text": "Hello from Bob via API!"},
    )
    assert resp.status_code == 201, f"Bob send message failed: {resp.text}"
    bob_msg_id = resp.json()["message_id"]
    print(f"Bob sent message: {bob_msg_id[:20]}...")

    # Charlie sends a message in his own network
    charlie_api.set_time(6200)
    resp = charlie_api.post(
        f"/api/networks/{u(charlie_network_id)}/channels/{u(charlie_channel_id)}/messages",
        json={"text": "Hello from Charlie via API!"},
    )
    assert resp.status_code == 201, f"Charlie send message failed: {resp.text}"
    charlie_msg_id = resp.json()["message_id"]
    print(f"Charlie sent message: {charlie_msg_id[:20]}...")

    db.commit()

    # Sync messages
    print("\n=== Sync messages ===")

    def all_messages_synced():
        # Alice should see both messages from her network
        resp = alice_api.get(f"/api/networks/{u(alice_network_id)}/channels/{u(alice_channel_id)}/messages")
        assert resp.status_code == 200, f"Alice list messages failed: {resp.text}"
        alice_messages = resp.json()["items"]
        alice_contents = [m["content"] for m in alice_messages]
        assert "Hello from Alice via API!" in alice_contents, "Alice should see her own message"
        assert "Hello from Bob via API!" in alice_contents, f"Alice should see Bob's message, got: {alice_contents}"

        # Bob should see both messages
        resp = bob_api.get(f"/api/networks/{u(alice_network_id)}/channels/{u(alice_channel_id)}/messages")
        assert resp.status_code == 200, f"Bob list messages failed: {resp.text}"
        bob_messages = resp.json()["items"]
        bob_contents = [m["content"] for m in bob_messages]
        assert "Hello from Alice via API!" in bob_contents, "Bob should see Alice's message"
        assert "Hello from Bob via API!" in bob_contents, f"Bob should see his own message, got: {bob_contents}"

    tick_helper.assert_eventually(all_messages_synced, db=db, start_t_ms=7000)
    print("Message sync completed")

    # === Test API: List messages ===
    print("\n=== API Test: List messages ===")

    # Verify final state
    resp = alice_api.get(f"/api/networks/{u(alice_network_id)}/channels/{u(alice_channel_id)}/messages")
    alice_messages = resp.json()["items"]
    alice_contents = [m["content"] for m in alice_messages]
    print(f"Alice sees {len(alice_messages)} messages: {alice_contents}")
    assert "Hello from Charlie via API!" not in alice_contents, "Alice should NOT see Charlie's message"

    resp = bob_api.get(f"/api/networks/{u(alice_network_id)}/channels/{u(alice_channel_id)}/messages")
    bob_messages = resp.json()["items"]
    bob_contents = [m["content"] for m in bob_messages]
    print(f"Bob sees {len(bob_messages)} messages: {bob_contents}")
    assert "Hello from Charlie via API!" not in bob_contents, "Bob should NOT see Charlie's message"

    # Charlie should only see his own message
    resp = charlie_api.get(f"/api/networks/{u(charlie_network_id)}/channels/{u(charlie_channel_id)}/messages")
    assert resp.status_code == 200, f"Charlie list messages failed: {resp.text}"
    charlie_messages = resp.json()["items"]
    charlie_contents = [m["content"] for m in charlie_messages]
    print(f"Charlie sees {len(charlie_messages)} messages: {charlie_contents}")

    assert "Hello from Charlie via API!" in charlie_contents, "Charlie should see his own message"
    assert "Hello from Alice via API!" not in charlie_contents, "Charlie should NOT see Alice's message"
    assert "Hello from Bob via API!" not in charlie_contents, "Charlie should NOT see Bob's message"

    # === Test API: List users ===
    print("\n=== API Test: List users ===")

    resp = alice_api.get(f"/api/networks/{u(alice_network_id)}/users")
    assert resp.status_code == 200, f"Alice list users failed: {resp.text}"
    users = resp.json()["items"]
    user_names = [usr.get("name") for usr in users]
    print(f"Users in Alice's network: {user_names}")

    assert "Alice" in user_names, "Alice should be in the user list"
    assert "Bob" in user_names, "Bob should be in the user list"
    assert "Charlie" not in user_names, "Charlie should NOT be in Alice's network"

    print("\n" + "=" * 50)
    print("ALL API TESTS PASSED!")
    print("=" * 50)
