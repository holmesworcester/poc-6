#!/bin/bash
# Build script for quiet_core Rust library
#
# Usage:
#   ./build.sh          # Development build
#   ./build.sh release  # Release build
#   ./build.sh test     # Run tests
#   ./build.sh check    # Run cargo check only

set -e

cd "$(dirname "$0")"

case "${1:-dev}" in
    dev|develop)
        echo "Building development version..."
        maturin develop
        echo "Done! Import with: import quiet_core"
        ;;

    release)
        echo "Building release version..."
        maturin develop --release
        echo "Done! Import with: import quiet_core"
        ;;

    test)
        echo "Running Rust tests..."
        cargo test

        echo ""
        echo "Running Python equivalence tests..."
        cd ..
        PYTHONPATH=. python -m pytest rust/tests/test_equivalence.py -v
        ;;

    check)
        echo "Running cargo check..."
        cargo check
        ;;

    clean)
        echo "Cleaning build artifacts..."
        cargo clean
        rm -rf target/
        ;;

    *)
        echo "Usage: $0 [dev|release|test|check|clean]"
        exit 1
        ;;
esac
