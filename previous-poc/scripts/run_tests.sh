#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip pyyaml rich pynacl textual >/dev/null

# Framework tests
python core/test_runner.py protocols/framework_tests

# SQL-first protocols use snapshot assertions
export SNAPSHOT_ONLY=1
python core/test_runner.py protocols/message_via_tor

# Other protocols (signed_groups currently mixed; snapshot gradually preferred)
unset SNAPSHOT_ONLY
python core/test_runner.py protocols/signed_groups

echo "All tests done."

