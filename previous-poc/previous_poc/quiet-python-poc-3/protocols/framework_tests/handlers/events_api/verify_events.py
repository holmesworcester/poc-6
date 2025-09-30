#!/usr/bin/env python3
"""Verify events API returns events with correct envelope format."""

import os
import sys
import json

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../../'))
sys.path.insert(0, project_root)

from core.handler_context import HandlerContext
from core.api_client import APIClient


def run(context: HandlerContext, parameters: dict):
    """Query the events API and verify the response format."""
    
    # Create API client
    api = APIClient("framework_tests")
    
    # Query the events API
    response = api.get("/__framework/events", {"limit": 100})
    
    if response.get('status') != 200:
        context.emit_event({
            'type': 'events_api_verification',
            'error': f"Events API returned status {response.get('status')}"
        })
        return {'error': 'Failed to query events API'}
    
    events = response.get('body', {}).get('events', [])
    
    # Check if events have the correct envelope format
    has_payload_field = True
    has_metadata_field = True
    first_event_type = None
    
    if events:
        first_event = events[0]
        has_payload_field = 'payload' in first_event
        has_metadata_field = 'metadata' in first_event
        
        # Get the type from the payload
        if has_payload_field:
            first_event_type = first_event.get('payload', {}).get('type')
        else:
            # Check if using incorrect 'data' field
            if 'data' in first_event:
                first_event_type = first_event.get('data', {}).get('type')
    
    # Emit verification event
    context.emit_event({
        'type': 'events_api_verification',
        'event_count': len(events),
        'has_payload_field': has_payload_field,
        'has_metadata_field': has_metadata_field,
        'first_event_type': first_event_type
    })
    
    # Also log details for debugging
    if not has_payload_field and events:
        print(f"WARNING: Events missing 'payload' field. First event structure: {json.dumps(events[0], indent=2)}")
    
    return {
        'verified': True,
        'event_count': len(events),
        'correct_format': has_payload_field and has_metadata_field
    }


if __name__ == '__main__':
    from core.handler_runner import run_command
    run_command(run)