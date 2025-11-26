#!/bin/bash
# Comprehensive CLI behavior test script
# Runs multiple scenarios and collects results for analysis

PYTHON="/home/hwilson/poc-6/venv/bin/python"
CLI="/home/hwilson/poc-6-cli-prototype/cli.py"
RESULTS_DIR="/tmp/cli_test_results"
mkdir -p "$RESULTS_DIR"

echo "========================================"
echo "CLI Behavior Test Suite"
echo "========================================"
echo ""

# Helper function to run a test scenario
run_test() {
    local name="$1"
    local commands="$2"
    local output_file="$RESULTS_DIR/${name}.txt"

    echo "[TEST] Running: $name"
    $PYTHON -u "$CLI" 2>&1 <<< "$commands" > "$output_file"
    echo "  ✓ Output saved to $output_file"
}

# Test 1: Single user - message doesn't appear
run_test "test_1_single_user_message" "
new-network --name alice --device desktop
send hello world
show
quit
"

# Test 2: Two users - bob receives message
run_test "test_2_two_user_sync" "
new-network --name alice --device desktop
create-invite
new-peer --name bob --device phone --invite 1
switch 1
send hello from alice
switch 2
sync --ticks 50
show
quit
"

# Test 3: List commands
run_test "test_3_list_commands" "
new-network --name alice --device desktop
create-invite
new-peer --name bob --device phone --invite 1
list-accounts
list-channels
list-users
time
quit
"

# Test 4: Channel creation and selection
run_test "test_4_channels" "
new-network --name alice --device desktop
create-channel random
create-channel random2
list-channels
select-channel 2
send in random
show
quit
"

# Test 5: Invalid commands
run_test "test_5_invalid_commands" "
new-network --name alice --device desktop
switch 999
select-channel 999
create-invite
invalid-command
quit
"

# Test 6: Multiple invites
run_test "test_6_multiple_peers" "
new-network --name alice --device desktop
create-invite
create-invite
new-peer --name bob --device phone --invite 1
new-peer --name charlie --device tablet --invite 2
list-accounts
quit
"

# Test 7: Set auto-tick off then on
run_test "test_7_auto_tick" "
new-network --name alice --device desktop
set-auto-tick off
send message 1
set-auto-tick on
send message 2
show
quit
"

# Test 8: Rapid switching
run_test "test_8_rapid_switching" "
new-network --name alice --device desktop
create-invite
new-peer --name bob --device phone --invite 1
switch 2
switch 1
switch 2
switch 1
show
quit
"

echo ""
echo "========================================"
echo "Analysis of Test Results"
echo "========================================"
echo ""

# Function to check for issues
check_issues() {
    local test_file="$1"
    local test_name=$(basename "$test_file" .txt)
    local issues=()
    local content=$(cat "$test_file")

    # Issue 1: Message doesn't appear after send
    if echo "$content" | grep -q "send hello world" && ! echo "$content" | grep -q "hello world" | grep -q "MAIN"; then
        issues+=("Message sent but doesn't appear in MAIN display")
    fi

    # Issue 2: Bob has network_???
    if echo "$content" | grep -q "bob.*phone.*network_???" ; then
        issues+=("Bob's network_id shows as '???' after joining")
    fi

    # Issue 3: Bob has no channels/users after joining
    if echo "$content" | grep -q "bob.*phone" && echo "$content" | grep -q "(no users)" && echo "$content" | grep -q "(no channels)"; then
        issues+=("Bob sees no users/channels after joining network")
    fi

    # Issue 4: Bob can't send message
    if echo "$content" | grep -q "Channel.*not found for peer"; then
        issues+=("Bob cannot send message - channel not found")
    fi

    # Issue 5: Messages never sync to other peers
    if echo "$content" | grep -q "send hello from alice" && ! echo "$content" | grep -q "hello from alice" | tail -5; then
        issues+=("Alice's message doesn't sync to bob")
    fi

    # Issue 6: Error messages in output
    if echo "$content" | grep -q "^error:"; then
        local error_count=$(echo "$content" | grep -c "^error:")
        issues+=("Found $error_count error message(s)")
    fi

    # Issue 7: Invalid command handling
    if echo "$content" | grep -q "invalid-command" && ! echo "$content" | grep -q "Unknown command"; then
        issues+=("Invalid command not properly rejected")
    fi

    if [ ${#issues[@]} -gt 0 ]; then
        echo "[$test_name]"
        for issue in "${issues[@]}"; do
            echo "  ⚠ $issue"
        done
    fi
}

# Analyze all test results
for test_file in "$RESULTS_DIR"/*.txt; do
    check_issues "$test_file"
done

echo ""
echo "========================================"
echo "Summary Statistics"
echo "========================================"
echo ""

for test_file in "$RESULTS_DIR"/*.txt; do
    local test_name=$(basename "$test_file" .txt)
    local lines=$(wc -l < "$test_file")
    local errors=$(grep -c "^error:" "$test_file" 2>/dev/null || echo 0)
    local warnings=$(grep -c "^warning:" "$test_file" 2>/dev/null || echo 0)
    local success_marks=$(grep -c "✓" "$test_file" 2>/dev/null || echo 0)

    printf "%-30s | lines: %5d | ✓: %2d | errors: %2d | warnings: %2d\n" \
        "$test_name" "$lines" "$success_marks" "$errors" "$warnings"
done

echo ""
echo "All results saved to: $RESULTS_DIR"
echo "To view a specific test: cat $RESULTS_DIR/test_N_*.txt"
