"""Tick system for running periodic jobs.

This module provides a simple tick() function that runs all recurring
operations in sequence. It's designed for deterministic testing where
we want to control exactly when jobs run.

Flow:
1. Establish/refresh connections with all known peers
2. Process incoming connects and sync responses
3. Send sync requests (uses established connections)
4. Process incoming sync responses
5. Purge expired connections
"""
from typing import Any
from events.transit import sync, sync_connect


def tick(t_ms: int, db: Any) -> None:
    """Run all periodic jobs for one tick cycle.

    This is a simple implementation that runs all jobs every tick,
    with no frequency control. Perfect for deterministic scenario tests.

    Args:
        t_ms: Current time in milliseconds
        db: Database connection
    """
    # Phase 1: Send connection announcements to establish/refresh connections
    sync_connect.send_connect_to_all(t_ms=t_ms, db=db)
    db.commit()

    # Phase 2: Process incoming (both connects and sync responses)
    sync.receive(batch_size=20, t_ms=t_ms, db=db)
    db.commit()

    # Phase 3: Send sync requests (uses established connections)
    sync.send_request_to_all(t_ms=t_ms, db=db)
    db.commit()

    # Phase 4: Process incoming sync responses
    sync.receive(batch_size=20, t_ms=t_ms, db=db)
    db.commit()

    # Phase 5: Purge expired connections
    sync_connect.purge_expired(t_ms=t_ms, db=db)
    db.commit()
