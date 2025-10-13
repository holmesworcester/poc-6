#!/bin/bash
# Standard test runner for poc-6
#
# Usage:
#   ./run_tests.sh                 # Run all tests
#   ./run_tests.sh tests/test_safe_scoping.py  # Run specific test file
#   ./run_tests.sh -k test_name    # Run tests matching pattern
#
# For LLMs: Use this script to run tests consistently

set -e  # Exit on error

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Set PYTHONPATH to current directory so imports work
export PYTHONPATH=.

# Run pytest with verbose output and stop on first failure
# -v: verbose
# -x: stop on first failure
# -s: show print statements
# Only run tests in the tests/ directory, ignore previous-poc/
pytest -xvs tests/ "$@"
