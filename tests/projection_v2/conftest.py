"""Pytest fixtures for projection v2 tests."""
import pytest
import sqlite3
from core.db import Database
from core import schema
from core import crypto
from events import registry


@pytest.fixture
def v2_db():
    """Create a fresh in-memory database for v2 projection tests.

    This is similar to fresh_db but kept separate to allow v2-specific
    setup if needed in the future.
    """
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


@pytest.fixture
def v2_network_state(v2_db):
    """Create minimal network state for v2 projection testing.

    Returns a dict with:
    - db: Database instance
    - network_id: ID of created network event
    - network_private_key: Private key for signing
    - peer_id: Local peer ID
    - network_pubkey: Public key bytes

    This bypasses the full bootstrap flow to create isolated test state.
    """
    from core.db import create_unsafe_db, create_safe_db
    from core import store

    db = v2_db
    unsafedb = create_unsafe_db(db)

    # Create local peer
    private_key, public_key = crypto.generate_keypair()
    peer_id = crypto.b64encode(crypto.hash(public_key))

    unsafedb.execute(
        "INSERT INTO local_peers (peer_id, private_key, public_key, created_at) VALUES (?, ?, ?, ?)",
        (peer_id, private_key, crypto.b64encode(public_key), 1000)
    )

    # Create network event
    network_private_key, network_public_key = crypto.generate_keypair()
    event_data = {
        'type': 'network',
        'network_pubkey': crypto.b64encode(network_public_key),
        'created_at': 1000
    }
    signed_event = crypto.sign_event(event_data, network_private_key)
    blob = crypto.canonicalize_json(signed_event)
    network_id = store.event(blob, peer_id, 1000, db)

    db.commit()

    return {
        'db': db,
        'network_id': network_id,
        'network_private_key': network_private_key,
        'network_pubkey': network_public_key,
        'peer_id': peer_id,
    }


@pytest.fixture
def register_event(monkeypatch):
    """Register a minimal event_spec/project_pure in the registry for tests."""
    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(registry, "_discovered", True)

    def _register(
        event_type: str,
        event_spec: dict,
        project_pure=None,
        shareable: bool = False,
        ephemeral: bool = False,
        projection_table: tuple[str, str] | None = None,
    ) -> None:
        registry._registry[event_type] = {
            "module": None,
            "module_name": "tests.projection_v2",
            "shareable": shareable,
            "ephemeral": ephemeral,
            "projection_table": projection_table,
            "event_spec": event_spec,
            "project_pure": project_pure,
        }

    return _register
