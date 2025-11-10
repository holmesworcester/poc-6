"""Tick system for running periodic jobs.

This module provides a simple tick() function that runs all recurring
operations in sequence. It's designed for deterministic testing where
we want to control exactly when jobs run.

Flow:
1. Send sync requests to all known peers
2. Receive and process incoming sync responses
3. Run message rekey and purge cycle (forward secrecy)
4. Purge expired events based on TTL (forward secrecy)
"""
from typing import Any
from events.transit import sync
from events.content import message_deletion
import purge_expired


def tick(t_ms: int, db: Any) -> None:
    """Run all periodic jobs for one tick cycle.

    This is a simple implementation that runs all jobs every tick,
    with no frequency control. Perfect for deterministic scenario tests.

    Args:
        t_ms: Current time in milliseconds
        db: Database connection
    """
    # Send sync requests from all local peers to all known peers
    sync.send_request_to_all(t_ms=t_ms, db=db)
    db.commit()

    # Receive and process incoming sync responses
    sync.receive(batch_size=20, t_ms=t_ms, db=db)
    db.commit()

    # Run message rekey and purge cycle for forward secrecy
    # This rekeys messages encrypted with keys marked for purging,
    # then purges those old keys
    message_deletion.run_message_purge_cycle_for_all_peers(t_ms=t_ms, db=db)
    db.commit()

    # Purge expired events (based on TTL) for forward secrecy
    # This removes expired prekeys, messages, and other TTL-based data
    purge_expired.run_purge_expired_for_all_peers(t_ms=t_ms, db=db)
    db.commit()
