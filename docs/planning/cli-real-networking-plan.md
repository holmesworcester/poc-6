# CLI with Real Networking

## Goal

Enable two CLI users on a local network to communicate with each other, with the functionality being self-QA-able by an LLM.

## Existing Foundation

**Already implemented:**
- **CLI with 40+ commands** - `new-network`, `join`, `send`, `create-invite`, `list-messages`, etc.
- **Interactive mode** (`--interactive`) - REPL with readline, tab completion
- **Non-interactive mode** - reads commands from stdin
- **Unified packet queue** (`core/queues.py`) - SQLite-based queue for all packet sources
- **Real networking tests** (`tests/networking/`) - UDP-based tests with separate databases per client
- **Network simulator** - stateless physics calculator for latency, loss, NAT

**What's missing:**
- `--exec "command"` - run single command and exit (vs reading all of stdin)
- `--json` - machine-parseable output for commands
- `--listen host:port` - real UDP networking in CLI

See: `docs/planning/network-context-and-server-mode.md`

---

## Design: Two Modes

### 1. Interactive Mode (Human Use)

```bash
# Terminal 1 - Alice
python cli.py --db alice.db --listen 0.0.0.0:9001

# Terminal 2 - Bob
python cli.py --db bob.db --listen 0.0.0.0:9002
```

**Flow**:
1. Alice creates network, generates invite
2. Alice shares invite link with Bob (copy/paste, QR code, etc.)
3. Bob joins via invite link
4. CLI discovers each other via peer addresses in events
5. UDP packets flow directly between them

**User Experience**:
- Standard CLI interface with REPL
- Background thread handles UDP recv
- Messages appear in real-time
- `> peers` command shows connected peers with latency

### 2. Non-Interactive Mode (LLM/Script Use)

```bash
# Execute single command and exit
python cli.py --db alice.db --exec "new-network Alice"
python cli.py --db alice.db --exec "create-invite" > invite.txt
python cli.py --db bob.db --exec "join $(cat invite.txt) Bob"

# Or pipe commands
echo "send Hello from Alice!" | python cli.py --db alice.db --channel general

# JSON output for parsing
python cli.py --db alice.db --exec "list-messages" --json
```

**Key Features**:
- `--exec "command"` - run single command, exit
- `--json` - output as JSON for machine parsing
- `--wait-for "condition"` - block until condition met
- `--timeout N` - max wait time in seconds
- Exit codes: 0=success, 1=error, 2=timeout

---

## LLM Self-QA Design

### Requirements

An LLM (like Claude) running `claude-code` should be able to:
1. Start two CLI instances in background
2. Create network on instance A
3. Generate invite on A, pass to B
4. Have B join the network
5. Send messages between A and B
6. Verify messages are received
7. All without human intervention

### Proposed Interface

```bash
# Start instances in background with named handles
python cli.py --db /tmp/alice.db --listen 127.0.0.1:9001 --daemon --name alice
python cli.py --db /tmp/bob.db --listen 127.0.0.1:9002 --daemon --name bob

# Send commands to named instances
cli-ctl alice "new-network Alice"
cli-ctl alice "create-invite" > /tmp/invite.txt
cli-ctl bob "join $(cat /tmp/invite.txt) Bob"

# Wait for sync
cli-ctl alice "wait-for 'bob in peers'" --timeout 10

# Send and verify messages
cli-ctl alice "send Hello Bob!"
cli-ctl bob "wait-for 'Hello Bob!' in messages" --timeout 5

# Check state
cli-ctl alice "list-messages --json" | jq '.[-1].content'

# Cleanup
cli-ctl alice "shutdown"
cli-ctl bob "shutdown"
```

### Alternative: Pure Bash with --exec

```bash
# Alice setup
python cli.py --db /tmp/alice.db --exec "new-network Alice" --json
INVITE=$(python cli.py --db /tmp/alice.db --exec "create-invite" --json | jq -r '.invite_link')

# Bob joins
python cli.py --db /tmp/bob.db --exec "join $INVITE Bob" --json

# Start background sync daemons
python cli.py --db /tmp/alice.db --listen 127.0.0.1:9001 --sync-only &
ALICE_PID=$!
python cli.py --db /tmp/bob.db --listen 127.0.0.1:9002 --sync-only &
BOB_PID=$!

# Wait and send
sleep 2  # Initial sync
python cli.py --db /tmp/alice.db --exec "send 'Hello Bob!'"

# Verify with retry
for i in {1..10}; do
  MSGS=$(python cli.py --db /tmp/bob.db --exec "list-messages" --json)
  if echo "$MSGS" | grep -q "Hello Bob"; then
    echo "SUCCESS: Bob received message"
    break
  fi
  sleep 1
done

# Cleanup
kill $ALICE_PID $BOB_PID
```

---

## Implementation Plan

### Phase 1: Add --exec and --json flags

Add to `cli.py` main():

```python
# New flags (existing: --interactive, --verbose, --quiet, --disk, --db-path)
parser.add_argument('--exec', help='Execute single command and exit')
parser.add_argument('--json', action='store_true', help='Output as JSON')
parser.add_argument('--listen', help='UDP listen address (host:port)')
```

**Commands already exist** - just need JSON output wrappers:
- `new-network --name X --username Y --devicename Z` ✓
- `create-invite` ✓
- `join --invite X --username Y --devicename Z` ✓
- `send <message>` ✓
- `messages` (list messages) ✓
- `channels` (list channels) ✓
- `users` (list users) ✓

### Phase 2: Background Sync Daemon

```python
# --sync-only mode
if args.sync_only:
    # Start UDP listener
    # Run tick loop
    # No REPL, just sync
    while True:
        tick(t_ms=current_time_ms(), db=db)
        time.sleep(0.1)
```

### Phase 3: Peer Discovery

When a peer starts listening:
1. Create `peer_address` event with `(peer_id, ip, port)`
2. Sync this event to network
3. Other peers see address, attempt UDP connection
4. Connection handshake establishes bidirectional communication

### Phase 4: LLM Testing Harness

Create `tests/llm_qa/` with:
- `test_two_party_chat.sh` - bash script an LLM can run
- `test_three_party_chat.sh` - three-way communication
- `test_file_transfer.sh` - file attachment sync
- `INSTRUCTIONS.md` - guide for LLM on how to run and interpret

---

## Open Questions

1. **Peer Discovery**: mDNS/Bonjour for LAN discovery, or require explicit IP:port?
   - Start with explicit addresses, add mDNS later

2. **NAT Traversal**: For LAN testing, not needed. For internet, need STUN/TURN.
   - LAN first, NAT traversal is future work

3. **Daemon Management**: How to cleanly start/stop background sync?
   - PID files? Unix sockets? Named pipes?
   - Start with simple PID files

4. **Condition Language**: What syntax for `wait-for`?
   - Simple: `wait-for "peers >= 1"`, `wait-for "messages contains 'hello'"`
   - Or just poll with `--exec` and let caller handle retry

---

## Success Criteria

1. **Two humans on LAN** can chat via CLI without any external server
2. **LLM running tests** can execute a script that:
   - Creates two CLI instances
   - Establishes communication
   - Sends messages both directions
   - Verifies receipt
   - Reports success/failure
3. **Tests are deterministic** - same script produces same result (within timeout)

---

## Related Work

- `tests/networking/` - Real UDP tests with separate DBs
- `tests/networking_tests/` - Additional UDP tests
- `core/queues.py` - Unified packet queue
- `simulator/nspy_network.py` - Network physics
