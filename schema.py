"""Schema initialization for all database tables."""
from typing import Any
import glob
import os


def create_all(db: Any) -> None:
    """Create all database tables by loading SQL files and creating cross-cutting tables.

    This function:
    1. Creates the shareable_events table (cross-cutting, not owned by a single module)
    2. Loads all .sql files from the root directory and events/ directory
    """
    # Create shareable_events table (cross-cutting table for sync)
    db.execute("""
        CREATE TABLE IF NOT EXISTS shareable_events (
            event_id TEXT PRIMARY KEY,
            peer_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    # Index for querying shareable events by peer
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_shareable_events_peer
        ON shareable_events(peer_id, created_at DESC)
    """)

    # Get the directory of this file
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Load all .sql files from root and events/ directory
    sql_files = []
    sql_files.extend(glob.glob(os.path.join(base_dir, '*.sql')))
    sql_files.extend(glob.glob(os.path.join(base_dir, 'events', '*.sql')))

    # Sort for deterministic order
    sql_files.sort()

    # Execute each SQL file
    for sql_file in sql_files:
        with open(sql_file, 'r') as f:
            sql_content = f.read()
            # Split on semicolons and execute each statement
            # (SQLite doesn't support multiple statements in one execute)
            for statement in sql_content.split(';'):
                statement = statement.strip()
                if statement:
                    db.execute(statement)

    # Commit all schema changes
    db.commit()
