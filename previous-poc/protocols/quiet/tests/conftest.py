"""
Test fixtures for Quiet protocol tests.
"""
import pytest
import sqlite3
import tempfile
import os
import sys
from pathlib import Path

# Add project root to path
protocol_dir = Path(__file__).parent.parent
project_root = protocol_dir.parent.parent
sys.path.insert(0, str(project_root))

from core.db import get_connection, init_database
from core.crypto import generate_keypair, sign
from core.pipeline import PipelineRunner


def process_envelope(envelope, db):
    """Helper to process a single envelope through the pipeline."""
    # Get the database path from the connection
    db_path = db.execute("PRAGMA database_list").fetchone()[2]
    runner = PipelineRunner(db_path=db_path, verbose=False)
    protocol_dir = Path(__file__).parent.parent
    runner.run(protocol_dir=str(protocol_dir), input_envelopes=[envelope])


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    yield db_path
    
    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def initialized_db(temp_db):
    """Create an initialized database with protocol schema."""
    conn = get_connection(temp_db)
    init_database(conn, str(protocol_dir))
    yield conn
    conn.close()


@pytest.fixture
def test_identity():
    """Generate a test identity with keypair."""
    private_key, public_key = generate_keypair()
    return {
        "private_key": private_key,
        "public_key": public_key,
        "peer_id": public_key.hex(),
        "network_id": "test-network"
    }


@pytest.fixture
def sample_identity_event(test_identity):
    """Create a sample identity event."""
    import json
    import time
    
    event = {
        "type": "peer_secret",
        "peer_id": test_identity["peer_id"],
        "network_id": test_identity["network_id"],
        "name": "Test User",
        "created_at": int(time.time() * 1000)
    }
    
    # Sign the event
    message = json.dumps(event, sort_keys=True).encode()
    signature = sign(message, test_identity["private_key"])
    event["signature"] = signature.hex()
    
    return event


@pytest.fixture
def sample_key_event(test_identity):
    """Create a sample key event."""
    import json
    import time
    
    event = {
        "type": "key",
        "key_id": "test-key-id",
        "group_id": "test-group",
        "created_by": test_identity["peer_id"],
        "network_id": test_identity["network_id"],
        "created_at": int(time.time() * 1000),
        "encrypted_key": "0" * 64  # Mock encrypted key
    }
    
    # Sign the event
    message = json.dumps(event, sort_keys=True).encode()
    signature = sign(message, test_identity["private_key"])
    event["signature"] = signature.hex()
    
    return event


# transit_secret event removed; per-request transit secrets are carried in sync_request plaintext
