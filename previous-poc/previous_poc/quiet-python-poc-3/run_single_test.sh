#!/bin/bash
export TEST_MODE=1
source venv/bin/activate
python core/test_runner.py protocols/signed_groups 2>&1 | grep -A50 "Auto-unblocks blocked message when author arrives" | head -100