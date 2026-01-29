#!/bin/bash
#
# Test two CLI clients communicating via UDP
#
# Run from project root:
#   bash tests/cli_networking/test_two_clients.sh
#
set -e

cd "$(dirname "$0")/../.."

echo "=== Two Client UDP Communication Test ==="
echo ""

# Clean up
rm -f /tmp/alice-test.db* /tmp/bob-test.db*

# Step 1: Alice creates network
echo "Step 1: Alice creates network"
PYTHONPATH=. python3 cli.py --db-path /tmp/alice-test.db --quiet \
    --exec "new-network --name TestNet --username Alice --devicename Laptop" \
    | grep -E "created|selected"
echo ""

# Step 2: Alice creates invite
echo "Step 2: Alice creates invite"
PYTHONPATH=. python3 cli.py --db-path /tmp/alice-test.db --quiet --exec "invite"
echo ""

# Step 3: Get Alice's peer_shared_id and invite link
echo "Step 3: Get Alice's identity"
ALICE_PEER=$(PYTHONPATH=. python3 cli.py --db-path /tmp/alice-test.db --quiet --exec "whoami" | grep peer_shared_id | awk '{print $2}')
INVITE=$(PYTHONPATH=. python3 cli.py --db-path /tmp/alice-test.db --quiet --exec "invite" | grep "link:" | awk '{print $2}')
echo "Alice peer_shared_id: $ALICE_PEER"
echo "Invite link: ${INVITE:0:50}..."
echo ""

# Step 4: Start Alice's sync daemon
echo "Step 4: Start Alice's daemon on 127.0.0.1:9001"
PYTHONPATH=. python3 cli.py --db-path /tmp/alice-test.db --listen 127.0.0.1:9001 --sync-only --quiet &
ALICE_PID=$!
echo "Alice daemon PID: $ALICE_PID"
sleep 1

# Verify it's running
if ! kill -0 $ALICE_PID 2>/dev/null; then
    echo "ERROR: Alice daemon failed to start"
    exit 1
fi
echo ""

# Step 5: Bob joins
echo "Step 5: Bob joins network (with Alice's address)"
PYTHONPATH=. python3 cli.py --db-path /tmp/bob-test.db --quiet \
    --listen 127.0.0.1:9002 \
    --peer "${ALICE_PEER}@127.0.0.1:9001" \
    --exec "accept-invite --username Bob --devicename Phone --invite $INVITE" \
    | grep -E "joined|selected|listening|added"
echo ""

# Step 6: Get Bob's peer_shared_id
echo "Step 6: Get Bob's identity"
BOB_PEER=$(PYTHONPATH=. python3 cli.py --db-path /tmp/bob-test.db --quiet --exec "whoami" | grep peer_shared_id | awk '{print $2}')
echo "Bob peer_shared_id: $BOB_PEER"
echo ""

# Step 7: Start Bob's daemon
echo "Step 7: Start Bob's daemon on 127.0.0.1:9002"
PYTHONPATH=. python3 cli.py --db-path /tmp/bob-test.db --listen 127.0.0.1:9002 \
    --peer "${ALICE_PEER}@127.0.0.1:9001" --sync-only --quiet &
BOB_PID=$!
echo "Bob daemon PID: $BOB_PID"
sleep 1
echo ""

# Step 8: Restart Alice with Bob's address
echo "Step 8: Restart Alice's daemon with Bob's address"
kill $ALICE_PID 2>/dev/null || true
sleep 1
PYTHONPATH=. python3 cli.py --db-path /tmp/alice-test.db --listen 127.0.0.1:9001 \
    --peer "${BOB_PEER}@127.0.0.1:9002" --sync-only --quiet &
ALICE_PID=$!
echo "Alice daemon PID: $ALICE_PID"
echo ""

# Step 9: Wait for initial sync
echo "Step 9: Waiting 5 seconds for initial sync..."
sleep 5
echo ""

# Step 10: Alice sends a message
echo "Step 10: Alice sends a message"
PYTHONPATH=. python3 cli.py --db-path /tmp/alice-test.db --quiet --exec "send Hello Bob from Alice!" \
    | grep -E "sent"
echo ""

# Step 11: Wait for message sync
echo "Step 11: Waiting 5 seconds for message sync..."
sleep 5
echo ""

# Step 12: Check Bob's messages
echo "Step 12: Check if Bob received the message"
BOB_MESSAGES=$(PYTHONPATH=. python3 cli.py --db-path /tmp/bob-test.db --quiet --exec "messages" 2>&1)
echo "Bob's messages:"
echo "$BOB_MESSAGES"
echo ""

# Cleanup
echo "=== Cleanup ==="
kill $ALICE_PID $BOB_PID 2>/dev/null || true
wait 2>/dev/null || true

# Verify
echo ""
echo "=== Result ==="
if echo "$BOB_MESSAGES" | grep -q "Hello Bob from Alice"; then
    echo "SUCCESS: Bob received Alice's message!"
    exit 0
else
    echo "FAILED: Bob did not receive the message"
    exit 1
fi
