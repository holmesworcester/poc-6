#!/usr/bin/env python3
"""Projector for events API test handler."""

import os
import sys

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../../'))
sys.path.insert(0, project_root)

from core.handler_context import HandlerContext


def project(context: HandlerContext, event: dict):
    """Project events API test events."""
    event_type = event.get('type')
    
    if event_type == 'test_event':
        # Store test events for verification
        context.state.setdefault('test_events', []).append(event)
    
    elif event_type == 'existing_event':
        # Store existing events for verification
        context.state.setdefault('existing_events', []).append(event)
    
    elif event_type == 'events_api_verification':
        # Store verification results
        context.state['last_verification'] = {
            'event_count': event.get('event_count', 0),
            'has_payload_field': event.get('has_payload_field', False),
            'has_metadata_field': event.get('has_metadata_field', False),
            'first_event_type': event.get('first_event_type')
        }


if __name__ == '__main__':
    from core.handler_runner import run_projector
    run_projector(project)