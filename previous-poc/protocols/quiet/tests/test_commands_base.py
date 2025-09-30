"""
Base class for command tests.
All command tests should verify:
1. Envelope structure (pure function test)
2. API response with IDs (pipeline integration test)
"""
import sqlite3
from typing import Dict, Any, List
from core.pipeline import PipelineRunner


class CommandTestBase:
    """Base class for command tests."""
    
    def run_command(self, command_name: str, params: Dict[str, Any], 
                    verbose: bool = False) -> Dict[str, str]:
        """Run a command through the pipeline and return stored event IDs."""
        runner = PipelineRunner(db_path=':memory:', verbose=verbose)
        result = runner.run('protocols/quiet', commands=[{
            'name': command_name,
            'params': params
        }])
        return result
    
    def assert_envelope_structure(self, envelope: Dict[str, Any], 
                                 expected_type: str,
                                 required_fields: List[str]) -> None:
        """Assert that an envelope has the expected structure."""
        assert envelope['event_type'] == expected_type, f"Expected type {expected_type}"
        assert 'event_plaintext' in envelope, "Missing event_plaintext"
        
        event = envelope['event_plaintext']
        assert event['type'] == expected_type, f"Event type mismatch"
        
        for field in required_fields:
            assert field in event, f"Missing required field: {field}"
    
    def assert_api_response(self, result: Dict[str, str], 
                           expected_types: List[str]) -> None:
        """Assert that the API response contains expected event types."""
        for event_type in expected_types:
            assert event_type in result, f"Missing {event_type} in result"
            assert len(result[event_type]) == 32, f"Invalid ID length for {event_type}"
