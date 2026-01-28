"""Tick system for running periodic jobs.

This module provides a simple tick() function that runs all recurring
operations from the jobs registry. Each job decides whether it should run
based on its own logic (frequency + custom conditions).

tick() maintains state in the job_state database table (survives process
restarts) and passes last_run_at to jobs as a parameter. Jobs are stateless
and deterministic.

Flow (order determined by jobs.JOBS registry):
1. Send connection announcements (sync_connect)
2. Receive incoming (messages from previous ticks, respecting deliver_at timestamps)
3. Send sync requests (added to incoming queue with deliver_at = t_ms + latency)
4. Purge expired connections
5. Message rekey and purge (forward secrecy)
6. Purge expired events (TTL-based)
7. Replenish prekeys when low

Note: Messages sent in tick N are delivered in tick N+1 or later based on
network latency configuration.
"""
from typing import Any
from .db import create_unsafe_db
from . import jobs


def reset_state(db: Any) -> None:
    """Reset tick state (for testing).

    Args:
        db: Database connection
    """
    unsafedb = create_unsafe_db(db)
    unsafedb.execute("DELETE FROM job_state")
    db.commit()


def tick(t_ms: int, db: Any) -> None:
    """Run jobs that determine they should run.

    For each job in jobs.JOBS:
    1. Get last run time from job_state table
    2. Ask if it should_run() given current time and last run time
    3. If yes, run() the job and update job_state

    State is persisted in the database so it survives process restarts
    (important for mobile where processes are frequently killed).

    Args:
        t_ms: Current time in milliseconds
        db: Database connection
    """
    from core import transport
    transport.set_simulator_time(t_ms)

    unsafedb = create_unsafe_db(db)

    for job in jobs.JOBS:
        # Get last run time from database (0 if never run)
        state = unsafedb.query_one(
            "SELECT last_run_at FROM job_state WHERE job_name = ?",
            (job.name,)
        )
        last_run_at = state['last_run_at'] if state else 0

        # Each job decides if it should run
        if job.should_run(t_ms, last_run_at, db):
            # Run the job
            job.run(t_ms, db)

            # Update state in database
            unsafedb.execute(
                """INSERT OR REPLACE INTO job_state (job_name, last_run_at, updated_at)
                   VALUES (?, ?, ?)""",
                (job.name, t_ms, t_ms)
            )
            db.commit()


# Jobs that are safe to run during sync-only mode (don't create new events)
SYNC_ONLY_JOBS = {
    'receive',
    'sync_respond',
    'sync_update',
    'high_priority_project',
    'low_priority_project',
    'unblock',  # Process blocked events queue
    'negentropy_sync',
    'connection_send',
}


def tick_sync_only(t_ms: int, db: Any) -> None:
    """Run only sync-related jobs (no event creation).

    This mode is used during sync_until_converged() to ensure convergence
    is achievable. Running event-creating jobs during sync would cause
    infinite sync loops as new events keep appearing.

    Jobs run in sync-only mode:
    - sync_receive: Process incoming messages (every 100ms)
    - negentropy_sync: Set reconciliation (every 1000ms)
    - connection_send: Send messages on connections (every 1000ms)

    Jobs NOT run (they create events):
    - intro_process, self_address_announce, transit_prekey_replenishment,
      group_prekey_replenishment, message_rekey_and_purge, etc.

    Args:
        t_ms: Current time in milliseconds
        db: Database connection
    """
    from core import transport
    transport.set_simulator_time(t_ms)

    unsafedb = create_unsafe_db(db)

    for job in jobs.JOBS:
        # Skip non-sync jobs
        if job.name not in SYNC_ONLY_JOBS:
            continue

        # Get last run time from database (0 if never run)
        state = unsafedb.query_one(
            "SELECT last_run_at FROM job_state WHERE job_name = ?",
            (job.name,)
        )
        last_run_at = state['last_run_at'] if state else 0

        # Respect job timing (don't force run - it's expensive)
        if job.should_run(t_ms, last_run_at, db):
            # Run the job
            job.run(t_ms, db)

            # Update state in database
            unsafedb.execute(
                """INSERT OR REPLACE INTO job_state (job_name, last_run_at, updated_at)
                   VALUES (?, ?, ?)""",
                (job.name, t_ms, t_ms)
            )
            db.commit()
