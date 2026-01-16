#!/bin/bash
# Comprehensive test runner for poc-6
#
# Usage:
#   ./run_tests.sh                 # Run all tests in parallel
#   ./run_tests.sh --python        # Run only Python tests
#   ./run_tests.sh --frontend      # Run only frontend tests
#   ./run_tests.sh --sequential    # Run all tests sequentially
#   ./run_tests.sh -k test_name    # Pass args to pytest
#
# Tests run in parallel:
#   - Python tests (pytest) - scenario tests, API tests, unit tests
#   - Frontend component tests (Cypress)
#   - Frontend E2E tests (Cypress + Vite dev server)

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Track exit codes
PYTHON_EXIT=0
COMPONENT_EXIT=0
E2E_EXIT=0

# Parse arguments
RUN_PYTHON=true
RUN_FRONTEND=true
SEQUENTIAL=false
PYTEST_ARGS=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --python)
            RUN_FRONTEND=false
            shift
            ;;
        --frontend)
            RUN_PYTHON=false
            shift
            ;;
        --sequential)
            SEQUENTIAL=true
            shift
            ;;
        *)
            PYTEST_ARGS="$PYTEST_ARGS $1"
            shift
            ;;
    esac
done

# Activate virtual environment if it exists
if [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
fi

export PYTHONPATH=.

# Temporary files for output
PYTHON_LOG=$(mktemp)
COMPONENT_LOG=$(mktemp)
E2E_LOG=$(mktemp)

cleanup() {
    rm -f "$PYTHON_LOG" "$COMPONENT_LOG" "$E2E_LOG"
    # Kill any background processes
    jobs -p | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT

run_python_tests() {
    echo -e "${BLUE}[Python]${NC} Starting pytest..."
    if pytest -v tests/ $PYTEST_ARGS > "$PYTHON_LOG" 2>&1; then
        PYTHON_EXIT=0
        echo -e "${GREEN}[Python]${NC} All tests passed!"
    else
        PYTHON_EXIT=1
        echo -e "${RED}[Python]${NC} Tests failed!"
    fi
}

run_component_tests() {
    if [ ! -d "app" ]; then
        echo -e "${YELLOW}[Component]${NC} No app directory, skipping frontend tests"
        return 0
    fi

    echo -e "${BLUE}[Component]${NC} Starting Cypress component tests..."
    cd app
    if npm run test > "$COMPONENT_LOG" 2>&1; then
        COMPONENT_EXIT=0
        echo -e "${GREEN}[Component]${NC} All tests passed!"
    else
        COMPONENT_EXIT=1
        echo -e "${RED}[Component]${NC} Tests failed!"
    fi
    cd ..
}

find_free_port() {
    # Find a free port starting from the given base
    local port=${1:-5173}
    while lsof -i:$port > /dev/null 2>&1; do
        port=$((port + 1))
        if [ $port -gt 5200 ]; then
            echo "5173"  # Fallback
            return
        fi
    done
    echo $port
}

run_e2e_tests() {
    if [ ! -d "app" ]; then
        return 0
    fi

    cd app

    # Find a free port to avoid conflicts with other worktrees
    VITE_PORT=$(find_free_port 5173)
    echo -e "${BLUE}[E2E]${NC} Starting Vite dev server on port $VITE_PORT..."

    # Start dev server in background on dynamic port
    npx vite --port $VITE_PORT > /dev/null 2>&1 &
    DEV_PID=$!

    # Wait for server to be ready
    for i in {1..30}; do
        if curl -s http://localhost:$VITE_PORT > /dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done

    echo -e "${BLUE}[E2E]${NC} Running Cypress E2E tests..."
    # Override baseUrl with dynamic port
    if CYPRESS_BASE_URL="http://localhost:$VITE_PORT" npm run test:e2e > "$E2E_LOG" 2>&1; then
        E2E_EXIT=0
        echo -e "${GREEN}[E2E]${NC} All tests passed!"
    else
        E2E_EXIT=1
        echo -e "${RED}[E2E]${NC} Tests failed!"
    fi

    # Stop dev server
    kill $DEV_PID 2>/dev/null || true
    cd ..
}

print_summary() {
    echo ""
    echo "============================================"
    echo "                TEST SUMMARY                "
    echo "============================================"

    if $RUN_PYTHON; then
        if [ $PYTHON_EXIT -eq 0 ]; then
            echo -e "${GREEN}[PASS]${NC} Python tests"
        else
            echo -e "${RED}[FAIL]${NC} Python tests"
            echo "       Log: $PYTHON_LOG"
        fi
    fi

    if $RUN_FRONTEND && [ -d "app" ]; then
        if [ $COMPONENT_EXIT -eq 0 ]; then
            echo -e "${GREEN}[PASS]${NC} Component tests"
        else
            echo -e "${RED}[FAIL]${NC} Component tests"
            echo "       Log: $COMPONENT_LOG"
        fi

        if [ $E2E_EXIT -eq 0 ]; then
            echo -e "${GREEN}[PASS]${NC} E2E tests"
        else
            echo -e "${RED}[FAIL]${NC} E2E tests"
            echo "       Log: $E2E_LOG"
        fi
    fi

    echo "============================================"

    # Show failed test output
    if [ $PYTHON_EXIT -ne 0 ] && [ -f "$PYTHON_LOG" ]; then
        echo ""
        echo -e "${RED}Python test failures:${NC}"
        tail -50 "$PYTHON_LOG"
    fi

    if [ $COMPONENT_EXIT -ne 0 ] && [ -f "$COMPONENT_LOG" ]; then
        echo ""
        echo -e "${RED}Component test failures:${NC}"
        tail -50 "$COMPONENT_LOG"
    fi

    if [ $E2E_EXIT -ne 0 ] && [ -f "$E2E_LOG" ]; then
        echo ""
        echo -e "${RED}E2E test failures:${NC}"
        tail -50 "$E2E_LOG"
    fi
}

# Main execution
echo "============================================"
echo "           RUNNING ALL TESTS               "
echo "============================================"
echo ""

if $SEQUENTIAL; then
    # Run sequentially
    $RUN_PYTHON && run_python_tests
    $RUN_FRONTEND && run_component_tests
    $RUN_FRONTEND && run_e2e_tests
else
    # Run in parallel
    if $RUN_PYTHON; then
        run_python_tests &
        PYTHON_PID=$!
    fi

    if $RUN_FRONTEND && [ -d "app" ]; then
        run_component_tests &
        COMPONENT_PID=$!
    fi

    # Wait for Python and Component tests
    if $RUN_PYTHON; then
        wait $PYTHON_PID || true
    fi

    if $RUN_FRONTEND && [ -d "app" ]; then
        wait $COMPONENT_PID || true
        # E2E runs after component (needs its own dev server)
        run_e2e_tests
    fi
fi

print_summary

# Exit with error if any tests failed
if [ $PYTHON_EXIT -ne 0 ] || [ $COMPONENT_EXIT -ne 0 ] || [ $E2E_EXIT -ne 0 ]; then
    exit 1
fi

echo ""
echo -e "${GREEN}All tests passed!${NC}"
exit 0
