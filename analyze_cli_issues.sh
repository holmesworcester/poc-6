#!/bin/bash
# Comprehensive CLI issue analysis script
# Runs in one go, produces detailed report

RESULTS_DIR="/tmp/cli_test_results"
REPORT="/tmp/cli_test_results/ISSUES_REPORT.md"

{
    echo "# CLI Behavior Analysis Report"
    echo ""
    echo "Generated: $(date)"
    echo ""
    echo "## Issues Discovered"
    echo ""

    # Issue 1: Messages don't appear after send
    echo "### Issue 1: Messages sent but don't appear in MAIN display"
    echo ""
    echo "**Status:** CONFIRMED"
    echo ""
    echo "**Evidence:**"
    if grep -q "send hello world" "$RESULTS_DIR/test_1_single_user_message.txt" && \
       ! grep -A5 "MAIN (#general):" "$RESULTS_DIR/test_1_single_user_message.txt" | grep -q "hello world"; then
        echo "- Test 1 (single user): Sent 'hello world' but message never appears in MAIN"
    fi
    echo ""
    echo "**Impact:** Users can send messages but won't see them in the UI. Critical UX issue."
    echo ""
    echo "**Expected behavior:** After sending a message, it should appear in the MAIN channel display immediately."
    echo ""
    echo "---"
    echo ""

    # Issue 2: Bob has network_???
    echo "### Issue 2: Invited peers show network_??? instead of actual network ID"
    echo ""
    echo "**Status:** CONFIRMED"
    echo ""
    echo "**Evidence:**"
    grep "network_???" "$RESULTS_DIR/test_2_two_user_sync.txt" | head -3 | sed 's/^/- /'
    echo ""
    echo "**Impact:** Cannot verify invited peers are properly joined to network. Data sync uncertainty."
    echo ""
    echo "**Root cause:** Backend issue - network_id not being set for invited peers after join. See TODO_NETWORK_ID_MISSING.md"
    echo ""
    echo "---"
    echo ""

    # Issue 3: Bob sees no channels/users after joining
    echo "### Issue 3: Invited peer (bob) cannot see any channels or users after joining"
    echo ""
    echo "**Status:** CONFIRMED"
    echo ""
    echo "**Evidence:**"
    echo "Test 2 output after bob joins:"
    grep -A6 "SIDEBAR (bob (phone)):" "$RESULTS_DIR/test_2_two_user_sync.txt" | head -8 | sed 's/^/  /'
    echo ""
    echo "**Impact:** Invited peers have no view of the network content. Complete feature breakage."
    echo ""
    echo "**Symptoms:**"
    echo "- \`(no users)\` in sidebar"
    echo "- \`(no channels)\` in sidebar"
    echo "- \`(channel not found)\` when trying to send messages"
    echo ""
    echo "---"
    echo ""

    # Issue 4: Bob can't send messages
    echo "### Issue 4: Invited peer (bob) cannot send messages - channel not found error"
    echo ""
    echo "**Status:** CONFIRMED (consequence of Issue 3)"
    echo ""
    echo "**Evidence:**"
    if grep -q "Channel.*not found for peer" "$RESULTS_DIR/test_2_two_user_sync.txt"; then
        echo "- Bob's send command fails with: 'Channel ... not found for peer ...'"
    fi
    echo ""
    echo "**Root cause:** Bob has no channels in their view (Issue 3)"
    echo ""
    echo "---"
    echo ""

    # Issue 5: Messages don't sync between peers
    echo "### Issue 5: Messages sent by alice don't appear on bob's side after sync"
    echo ""
    echo "**Status:** CONFIRMED"
    echo ""
    echo "**Evidence:**"
    echo "Test 2: Alice sends 'hello from alice', bob syncs, alice views:"
    grep "hello from alice" "$RESULTS_DIR/test_2_two_user_sync.txt" && echo "  ✓ Found in sent commands" || echo "  ✗ Never appears in received messages"
    echo ""
    echo "**Impact:** Core messaging feature is broken - messages don't propagate."
    echo ""
    echo "---"
    echo ""

    # Issue 6: list-users fails when not in network
    echo "### Issue 6: list-users command fails with 'not in a network' error"
    echo ""
    echo "**Status:** CONFIRMED"
    echo ""
    echo "**Evidence:**"
    grep "list-users" "$RESULTS_DIR/test_3_list_commands.txt" -A1 | sed 's/^/- /'
    echo ""
    echo "**Context:** Bob (invited peer) tries list-users after joining"
    echo ""
    echo "**Problem:** Bob has network_??? which causes backend to reject the query. Related to Issue 2."
    echo ""
    echo "---"
    echo ""

    # Issue 7: Channels appear but empty
    echo "### Issue 7: Channels created successfully but messages don't appear in them"
    echo ""
    echo "**Status:** CONFIRMED"
    echo ""
    echo "**Evidence:**"
    grep -A8 "MAIN (#random):" "$RESULTS_DIR/test_4_channels.txt" | head -2 | sed 's/^/- /'
    echo ""
    echo "Test 4: Creates 'random' channel, sends 'in random', but MAIN shows '(no messages)'"
    echo ""
    echo "---"
    echo ""

    # Issue 8: Invalid command handling works correctly
    echo "### Issue 8: Error handling - WORKING CORRECTLY"
    echo ""
    echo "**Status:** OK"
    echo ""
    echo "**Evidence:**"
    grep -E "(account #999|channel #999|unknown command)" "$RESULTS_DIR/test_5_invalid_commands.txt" | sed 's/^/- /'
    echo ""
    echo "Error messages are clear and helpful."
    echo ""
    echo "---"
    echo ""

    # Summary
    echo "## Summary"
    echo ""
    echo "| Issue | Severity | Component | Status |"
    echo "|-------|----------|-----------|--------|"
    echo "| Messages don't appear after send | CRITICAL | Frontend display | Needs investigation |"
    echo "| network_??? for invited peers | HIGH | Backend sync | TODO_NETWORK_ID |"
    echo "| Invited peer sees no channels/users | CRITICAL | Backend sync/projection | Blocker for multi-user |"
    echo "| Invited peer can't send messages | CRITICAL | Channel lookup | Blocked by issue above |"
    echo "| Messages don't sync between peers | CRITICAL | Event sync | Core feature broken |"
    echo "| list-users fails for invited peers | HIGH | Backend query | Blocked by network_??? |"
    echo "| Channels created but empty | CRITICAL | Message projection | Sync issue |"
    echo ""
    echo "## Next Steps"
    echo ""
    echo "1. **Highest priority:** Fix invited peer channel/user visibility (Issue 3)"
    echo "   - Investigate backend projection for new peers"
    echo "   - Check if channels/groups are being shared correctly"
    echo ""
    echo "2. **High priority:** Fix messages appearing in MAIN display (Issue 1)"
    echo "   - Check message.list() backend function"
    echo "   - Verify projection is working"
    echo ""
    echo "3. **High priority:** Investigate network_id being NULL for invited peers (Issue 2)"
    echo "   - See existing TODO_NETWORK_ID_MISSING.md"
    echo ""

} > "$REPORT"

cat "$REPORT"
echo ""
echo "Full report saved to: $REPORT"
