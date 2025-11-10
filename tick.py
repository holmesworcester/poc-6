"""Tick system for running periodic jobs.

This module provides a simple tick() function that runs all recurring
operations in sequence. It's designed for deterministic testing where
we want to control exactly when jobs run.
"""
from typing import Any
from events.transit import sync


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
