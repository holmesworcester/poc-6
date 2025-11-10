"""Job registry for periodic operations.

This module defines all jobs that should run periodically. Currently it's
a simple list, but can be extended to support frequency control, job state
persistence, and other features as needed.
"""
from events.transit import sync, sync_connect


# Job registry - list of all periodic jobs
# Each job is a dict with:
#   - name: Unique identifier for the job
#   - fn: Function to call
#   - params: Default parameters (can be overridden)
#   - every_ms: How often to run (not currently enforced, for future use)
JOBS = [
    {
        'name': 'connect_send',
        'fn': sync_connect.send_connect_to_all,
        'params': {},
        'every_ms': 60_000,  # 1 minute (informational, not enforced)
    },
    {
        'name': 'sync_send',
        'fn': sync.send_request_to_all,
        'params': {},
        'every_ms': 5_000,  # 5 seconds (informational, not enforced)
    },
    {
        'name': 'sync_receive',
        'fn': sync.receive,
        'params': {'batch_size': 20},
        'every_ms': 5_000,  # 5 seconds (informational, not enforced)
    },
    {
        'name': 'connection_purge',
        'fn': sync_connect.purge_expired,
        'params': {},
        'every_ms': 60_000,  # 1 minute (informational, not enforced)
    },
    # Future jobs:
    # - transit_prekey_replenishment (every 1-6 hours)
    # - group_prekey_replenishment (every 1-6 hours)
]
