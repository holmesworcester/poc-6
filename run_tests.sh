#!/bin/bash
# Comprehensive test runner for poc-6
#
# Usage:
#   ./run_tests.sh                 # Run all tests in parallel (Python + Frontend)
#   ./run_tests.sh --python        # Run only Python tests
#   ./run_tests.sh --frontend      # Run only frontend tests
#   ./run_tests.sh --sequential    # Run all tests sequentially
#   ./run_tests.sh -k test_name    # Pass args to pytest
#
# Tests run in parallel:
#   - Python tests (pytest) - scenario tests, API tests, unit tests
#   - Frontend component tests (Cypress)
#   - Frontend E2E tests (Cypress + Vite dev server)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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

# Temporary files for output and exit codes
TMPDIR=$(mktemp -d)
PYTHON_LOG="$TMPDIR/python.log"
COMPONENT_LOG="$TMPDIR/component.log"
E2E_LOG="$TMPDIR/e2e.log"
PYTHON_EXIT_FILE="$TMPDIR/python.exit"
COMPONENT_EXIT_FILE="$TMPDIR/component.exit"
E2E_EXIT_FILE="$TMPDIR/e2e.exit"

# Initialize exit files
echo "0" > "$PYTHON_EXIT_FILE"
echo "0" > "$COMPONENT_EXIT_FILE"
echo "0" > "$E2E_EXIT_FILE"

cleanup() {
    rm -rf "$TMPDIR"
    # Kill any background processes
    jobs -p | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT

find_free_port() {
    local port=${1:-5173}
    while lsof -i:$port > /dev/null 2>&1; do
        port=$((port + 1))
        if [ $port -gt 5200 ]; then
            echo "5173"
            return
        fi
    done
    echo $port
}

run_python_tests() {
    echo -e "${BLUE}[Python]${NC} Starting pytest..."
    if pytest -v tests/ $PYTEST_ARGS > "$PYTHON_LOG" 2>&1; then
        echo "0" > "$PYTHON_EXIT_FILE"
        echo -e "${GREEN}[Python]${NC} All tests passed!"
    else
        echo "1" > "$PYTHON_EXIT_FILE"
        echo -e "${RED}[Python]${NC} Tests failed!"
    fi
}

run_component_tests() {
    if [ ! -d "app" ]; then
        echo -e "${YELLOW}[Component]${NC} No app directory, skipping"
        return 0
    fi

    echo -e "${BLUE}[Component]${NC} Starting Cypress component tests..."
    pushd app > /dev/null
    if npm run test > "$COMPONENT_LOG" 2>&1; then
        echo "0" > "$COMPONENT_EXIT_FILE"
        echo -e "${GREEN}[Component]${NC} All tests passed!"
    else
        echo "1" > "$COMPONENT_EXIT_FILE"
        echo -e "${RED}[Component]${NC} Tests failed!"
    fi
    popd > /dev/null
}

run_e2e_tests() {
    if [ ! -d "app" ]; then
        return 0
    fi

    pushd app > /dev/null

    # Find a free port to avoid conflicts with other worktrees
    VITE_PORT=$(find_free_port 5173)
    echo -e "${BLUE}[E2E]${NC} Starting Vite on port $VITE_PORT..."

    # Start dev server in background
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
    if CYPRESS_BASE_URL="http://localhost:$VITE_PORT" npm run test:e2e > "$E2E_LOG" 2>&1; then
        echo "0" > "$E2E_EXIT_FILE"
        echo -e "${GREEN}[E2E]${NC} All tests passed!"
    else
        echo "1" > "$E2E_EXIT_FILE"
        echo -e "${RED}[E2E]${NC} Tests failed!"
    fi

    kill $DEV_PID 2>/dev/null || true
    popd > /dev/null
}

print_summary() {
    local PYTHON_EXIT=$(cat "$PYTHON_EXIT_FILE")
    local COMPONENT_EXIT=$(cat "$COMPONENT_EXIT_FILE")
    local E2E_EXIT=$(cat "$E2E_EXIT_FILE")

    echo ""
    echo "============================================"
    echo "                TEST SUMMARY                "
    echo "============================================"

    if $RUN_PYTHON; then
        if [ "$PYTHON_EXIT" -eq 0 ]; then
            echo -e "${GREEN}[PASS]${NC} Python tests"
        else
            echo -e "${RED}[FAIL]${NC} Python tests"
        fi
    fi

    if $RUN_FRONTEND && [ -d "app" ]; then
        if [ "$COMPONENT_EXIT" -eq 0 ]; then
            echo -e "${GREEN}[PASS]${NC} Component tests (31 tests)"
        else
            echo -e "${RED}[FAIL]${NC} Component tests"
        fi

        if [ "$E2E_EXIT" -eq 0 ]; then
            echo -e "${GREEN}[PASS]${NC} E2E tests (10 tests)"
        else
            echo -e "${RED}[FAIL]${NC} E2E tests"
        fi
    fi

    echo "============================================"

    # Show failed test output
    if [ "$PYTHON_EXIT" -ne 0 ] && [ -f "$PYTHON_LOG" ]; then
        echo -e "\n${RED}Python test failures:${NC}"
        tail -50 "$PYTHON_LOG"
    fi

    if [ "$COMPONENT_EXIT" -ne 0 ] && [ -f "$COMPONENT_LOG" ]; then
        echo -e "\n${RED}Component test failures:${NC}"
        tail -50 "$COMPONENT_LOG"
    fi

    if [ "$E2E_EXIT" -ne 0 ] && [ -f "$E2E_LOG" ]; then
        echo -e "\n${RED}E2E test failures:${NC}"
        tail -50 "$E2E_LOG"
    fi

    # Return overall exit code
    if [ "$PYTHON_EXIT" -ne 0 ] || [ "$COMPONENT_EXIT" -ne 0 ] || [ "$E2E_EXIT" -ne 0 ]; then
        return 1
    fi
    return 0
}

# Main execution
echo "============================================"
echo "           RUNNING ALL TESTS               "
echo "============================================"
echo ""

START_TIME=$(date +%s)

if $SEQUENTIAL; then
    # Run sequentially
    $RUN_PYTHON && run_python_tests
    $RUN_FRONTEND && run_component_tests
    $RUN_FRONTEND && run_e2e_tests
else
    # Run Python and Component tests in parallel
    if $RUN_PYTHON; then
        run_python_tests &
        PYTHON_PID=$!
    fi

    if $RUN_FRONTEND && [ -d "app" ]; then
        run_component_tests &
        COMPONENT_PID=$!
    fi

    # Wait for parallel tests
    $RUN_PYTHON && wait $PYTHON_PID
    $RUN_FRONTEND && [ -d "app" ] && wait $COMPONENT_PID

    # E2E runs after component (needs dev server)
    $RUN_FRONTEND && run_e2e_tests
fi

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

if print_summary; then
    echo ""
    echo -e "${GREEN}All tests passed in ${ELAPSED}s!${NC}"
    exit 0
else
    echo ""
    echo -e "${RED}Some tests failed (${ELAPSED}s)${NC}"
    exit 1
fi
