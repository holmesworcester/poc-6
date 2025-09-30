#!/usr/bin/env python3
"""Create a test event for verifying the events API."""

import os
import sys

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../../'))
sys.path.insert(0, project_root)

from core.handler_context import HandlerContext


def run(context: HandlerContext, parameters: dict):
    """Create a test event."""
    event_type = parameters.get('event_type', 'test_event')
    event_data = parameters.get('event_data', {})
    
    # Create the event
    context.emit_event({
        'type': event_type,
        **event_data
    })
    
    return {
        'created': True,
        'event_type': event_type
    }


if __name__ == '__main__':
    from core.handler_runner import run_command
    run_command(run)