#!/usr/bin/env python3
"""Quickly replay a saved convergence failure for debugging.

Usage:
    python tests/replay_failure.py tests/failures/convergence_failure_1234567890_ordering_1.json

    # Or with verbose logging
    PYTHONPATH=. python tests/replay_failure.py tests/failures/convergence_failure_1234567890_ordering_1.json --log-level=DEBUG
"""

import sys
import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description='Replay a saved convergence failure')
    parser.add_argument('failure_file', help='Path to failure JSON file')
    parser.add_argument('--log-level', default='INFO',
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       help='Logging level (default: INFO)')

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s'
    )

    # Import after logging setup
    import sqlite3
    from db import Database
    import schema
    from tests.utils.convergence import replay_from_file

    print(f"Setting up test database...")
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # NOTE: This script assumes the failure file contains enough context
    # to replay. For now, it only works if the test data is recreated manually.
    # Future enhancement: Save test setup code in the failure file.

    print(f"\n⚠️  WARNING: You must manually recreate the test data before replaying.")
    print(f"   The failure file only contains event orderings, not the original test data.\n")

    # TODO: Load and execute test setup from failure file if available

    # Replay the failure
    try:
        replay_from_file(db, args.failure_file)
        print("\n✅ Replay succeeded! The failure has been fixed.")
        return 0
    except AssertionError as e:
        print(f"\n❌ Replay failed with same error: {e}")
        return 1
    except FileNotFoundError:
        print(f"\n❌ Failure file not found: {args.failure_file}")
        return 1
    except Exception as e:
        print(f"\n❌ Unexpected error during replay: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
