"""Schema initialization for all database tables."""
from typing import Any
import glob
import os


def create_all(db: Any) -> None:
    """Create all database tables by loading SQL files.

    Loads all .sql files from the root directory and events/ directory (including subdirectories).
    """
    # Get the directory of this file
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Load all .sql files from root and events/ directory (including subdirs)
    sql_files = []
    sql_files.extend(glob.glob(os.path.join(base_dir, '*.sql')))
    sql_files.extend(glob.glob(os.path.join(base_dir, 'events', '*.sql')))
    sql_files.extend(glob.glob(os.path.join(base_dir, 'events', '**', '*.sql'), recursive=True))

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
