"""
Protocol-specific jobs configuration for Quiet.
"""

JOBS = [
    {
        'op': 'sync_request.run',
        'params': {},
        'every_ms': 5_000,
    },
]

