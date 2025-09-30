#!/usr/bin/env python3
"""
Minimal test to see what's needed for pipeline to work.
"""
import sys
import tempfile
import hashlib
from pathlib import Path

from pathlib import Path
this_file = Path(__file__).resolve()
protocol_dir = this_file.parents[2]  # .../protocols/quiet
repo_root = this_file.parents[4]
sys.path.insert(0, str(repo_root))

from core.pipeline import PipelineRunner
from core.db import get_connection

def test_minimal_pipeline():
    """Test with a minimal pre-signed envelope."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        
        # Create a minimal signed envelope
        event_id = hashlib.blake2b(b"test-event").hexdigest()
        envelope = {
            'event_type': 'network',
            'event_plaintext': {
                'type': 'network',
                'network_id': event_id,  # Use event_id as network_id
                'name': 'Test Network',
                'description': 'Test',
                'creator_id': 'test-creator',
                'created_at': 1234567890,
                'signature': 'fake-signature'
            },
            'self_created': True,
            'peer_id': 'test-creator',
            'network_id': event_id,
            'deps': [],
            # Add fields that handlers expect
            'event_id': event_id,
            'sig_checked': True,  # Pretend signature was checked
            'validated': True,   # Pretend it's validated
            'projected': False
        }
        
        # Run pipeline
        runner = PipelineRunner(db_path=str(db_path), verbose=True)
        
        # Check which handlers are registered
        from core.handlers import registry
        print(f"Handlers before loading: {[h.name for h in registry._handlers]}")
        
        # Add more detailed logging
        import logging
        logging.basicConfig(level=logging.DEBUG)
        
        runner.run(protocol_dir=str(protocol_dir), input_envelopes=[envelope])
        
        print(f"\nHandlers after loading: {[h.name for h in registry._handlers]}")
        
        # Check database
        db = get_connection(str(db_path))
        
        # Check networks table
        cursor = db.execute("SELECT * FROM networks")
        networks = cursor.fetchall()
        print(f"\nNetworks: {len(networks)}")
        for net in networks:
            print(f"  {dict(net)}")
        
        db.close()

if __name__ == "__main__":
    test_minimal_pipeline()
