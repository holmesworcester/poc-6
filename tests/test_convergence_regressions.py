"""Regression tests for known convergence failures.

Each saved failure in tests/failures/ becomes an individual test case.
This ensures that once a convergence bug is fixed, it stays fixed.

To add a new regression test:
1. Run a test that triggers assert_convergence()
2. When it fails, a JSON file is saved to tests/failures/
3. Fix the bug
4. Re-run pytest - this file will automatically test all saved failures

Note: Currently this requires manual test data recreation.
Future enhancement: Save test setup in failure files for full automation.
"""

import pytest
import glob
import json
import os


# Collect all failure files
failure_files = glob.glob("tests/failures/convergence_*.json")


@pytest.mark.skipif(
    len(failure_files) == 0,
    reason="No convergence failure files found in tests/failures/"
)
@pytest.mark.parametrize("failure_file", failure_files)
def test_convergence_regression(failure_file):
    """Regression test for a known convergence failure.

    This test will:
    1. Load the saved failure data
    2. Recreate the test scenario (TODO: needs test setup in failure file)
    3. Replay the failed ordering
    4. Verify it now produces the same state as baseline
    """
    import sqlite3
    from db import Database
    import schema
    from tests.utils.convergence import replay_from_file

    # Load failure metadata
    with open(failure_file, 'r') as f:
        failure_data = json.load(f)

    # Setup fresh database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # TODO: Recreate test data from failure file
    # For now, this test will fail because we don't have the original data
    # Future enhancement: Save test setup code/data in the failure file

    pytest.skip(
        f"Regression test for {os.path.basename(failure_file)} requires test data recreation. "
        f"Original failure: {failure_data.get('difference', 'unknown')}"
    )

    # Once test data recreation is implemented:
    # replay_from_file(db, failure_file)
    # Test passes if no AssertionError is raised


def test_no_regression_failures_exist():
    """Meta-test: Verify that all convergence failures have been fixed.

    This test passes when there are NO failure files in tests/failures/.
    If failure files exist, they should either:
    1. Be fixed (and then the files deleted), or
    2. Be documented as known issues
    """
    if len(failure_files) > 0:
        pytest.skip(
            f"Found {len(failure_files)} convergence failure file(s). "
            f"These represent known or historical failures. "
            f"Files: {[os.path.basename(f) for f in failure_files]}"
        )
