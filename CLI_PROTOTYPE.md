# CLI Prototype Design

## Design Principles

### CRITICAL RULE: Function-Only API Access
**THIS RULE MUST NEVER BE BROKEN**

The CLI must:
- Use ONLY the event functions from `events/` modules (e.g., `user.new_network()`, `message.create()`, etc.)
- NEVER access the database directly (no raw SQL queries from CLI code)
- Follow the same patterns as scenario tests in `tests/scenario_tests/`
- Think of this as an API client - the event functions ARE the API

This ensures:
1. All business logic stays in event functions
2. CLI remains maintainable as functions evolve
3. Similar constraints to a real API client
4. Consistent behavior with scenario tests

### Isomorphic Commands
Commands should work identically in interactive and non-interactive modes:
- Same command syntax
- Same output format
- Same state display between commands

### State Display Format
State should be shown after each command in a human and LLM-readable format:

```
┌─────────────────────────────────────────────────────────
│ PEER: Alice (alice_peer_id_abc123...)
│ USER: Alice (alice_user_id_xyz789...)
│ NETWORK: network_id_def456...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (group_id_ghi012...)
│     Members: Alice
│   • admins (group_id_jkl345...)
│     Members: Alice
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (channel_id_mno678...)
│     Messages (0):
├─────────────────────────────────────────────────────────
│ INVITES (0):
└─────────────────────────────────────────────────────────
```

## Interactive Mode

### Starting the CLI
```bash
$ ./cli.py --interactive
Welcome to POC-6 CLI (Interactive Mode)

Active peer: (none)
>
```

### Session Flow Example: Three Player Messaging

```
> new-network --name Alice
✓ Created network as Alice
✓ Switched to peer: Alice

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ USER: Alice (nF7kRm...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice
│   • admins (8KwNqY...)
│     Members: Alice
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (0):
├─────────────────────────────────────────────────────────
│ INVITES (0):
└─────────────────────────────────────────────────────────

> create-invite
✓ Created invite: invite_vR9sLp...
✓ Invite link: poc6://invite/eyJpbnZpdGVfaWQiOiJ2Ujlz...

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ USER: Alice (nF7kRm...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...) - 1 member
│   • admins (8KwNqY...) - 1 member
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (0):
├─────────────────────────────────────────────────────────
│ INVITES (1):
│   • invite_vR9sLp... (created by Alice)
└─────────────────────────────────────────────────────────

> new-peer --name Bob --invite poc6://invite/eyJpbnZpdGVfaWQiOiJ2Ujlz...
✓ Created peer Bob
✓ Joined network uA2vWy... as Bob
✓ Switched to peer: Bob

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ USER: Bob (wP8qLx...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...) - 1 member (pre-sync)
│   • admins (8KwNqY...) - 1 member (pre-sync)
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (0):
├─────────────────────────────────────────────────────────
│ INVITES (0):
└─────────────────────────────────────────────────────────

> switch Alice
✓ Switched to peer: Alice

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ ... (Alice's current state)
└─────────────────────────────────────────────────────────

> sync --rounds 15
✓ Synced 15 rounds (t=4000ms -> 7000ms)

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ USER: Alice (nF7kRm...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice, Bob
│   • admins (8KwNqY...)
│     Members: Alice
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (0):
├─────────────────────────────────────────────────────────
│ INVITES (1):
│   • invite_vR9sLp... (created by Alice)
└─────────────────────────────────────────────────────────

> switch Bob
✓ Switched to peer: Bob

> send --channel general --message "Hello from Bob!"
✓ Sent message: msg_yH4nVw...

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ USER: Bob (wP8qLx...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...) - 2 members
│   • admins (8KwNqY...) - 1 member
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (1):
│       [5100ms] Bob: Hello from Bob!
├─────────────────────────────────────────────────────────
│ INVITES (1):
│   • invite_vR9sLp...
└─────────────────────────────────────────────────────────

> switch Alice
✓ Switched to peer: Alice

> send --channel general --message "Hello from Alice!"
✓ Sent message: msg_pQ2rXs...

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (1):
│       [5000ms] Alice: Hello from Alice!
└─────────────────────────────────────────────────────────

> sync --rounds 20
✓ Synced 20 rounds (t=7000ms -> 11000ms)

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (2):
│       [5100ms] Bob: Hello from Bob!
│       [5000ms] Alice: Hello from Alice!
└─────────────────────────────────────────────────────────

> switch Bob

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (2):
│       [5100ms] Bob: Hello from Bob!
│       [5000ms] Alice: Hello from Alice!
└─────────────────────────────────────────────────────────

> list-peers
Active peers in session:
  * Bob (tD5mNk...) - USER: Bob (wP8qLx...)
  - Alice (gBQNZX...) - USER: Alice (nF7kRm...)

> show-all
┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ USER: Alice (nF7kRm...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice, Bob
│   • admins (8KwNqY...)
│     Members: Alice
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (2):
│       [5100ms] Bob: Hello from Bob!
│       [5000ms] Alice: Hello from Alice!
├─────────────────────────────────────────────────────────
│ INVITES (1):
│   • invite_vR9sLp... (created by Alice)
└─────────────────────────────────────────────────────────

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ USER: Bob (wP8qLx...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice, Bob
│   • admins (8KwNqY...)
│     Members: Alice
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (2):
│       [5100ms] Bob: Hello from Bob!
│       [5000ms] Alice: Hello from Alice!
├─────────────────────────────────────────────────────────
│ INVITES (1):
│   • invite_vR9sLp... (created by Alice)
└─────────────────────────────────────────────────────────

> quit
Goodbye!
```

## Non-Interactive Mode

### Command Syntax
Commands are provided as CLI arguments. State is shown after each command.

### Example: Three Player Messaging
```bash
$ ./cli.py \
  --new-network Alice \
  --create-invite \
  --new-peer Bob --join-with-last-invite \
  --sync 15 \
  --switch Bob \
  --send "Hello from Bob!" \
  --switch Alice \
  --send "Hello from Alice!" \
  --sync 20 \
  --show-all

[1] new-network --name Alice
✓ Created network as Alice
✓ Switched to peer: Alice

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ USER: Alice (nF7kRm...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice
│   • admins (8KwNqY...)
│     Members: Alice
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (0):
├─────────────────────────────────────────────────────────
│ INVITES (0):
└─────────────────────────────────────────────────────────

[2] create-invite
✓ Created invite: invite_vR9sLp...
✓ Invite link: poc6://invite/eyJpbnZpdGVfaWQiOiJ2Ujlz...

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
├─────────────────────────────────────────────────────────
│ INVITES (1):
│   • invite_vR9sLp... (created by Alice)
└─────────────────────────────────────────────────────────

[3] new-peer --name Bob --join-with-last-invite
✓ Created peer Bob
✓ Joined network uA2vWy... as Bob
✓ Switched to peer: Bob

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ USER: Bob (wP8qLx...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...) - 1 member (pre-sync)
│   • admins (8KwNqY...) - 1 member (pre-sync)
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (0):
├─────────────────────────────────────────────────────────
│ INVITES (0):
└─────────────────────────────────────────────────────────

[4] sync --rounds 15
✓ Synced 15 rounds (t=4000ms -> 7000ms)

┌─────────────────────────────────────────────────────────
│ ALL PEERS STATE AFTER SYNC
└─────────────────────────────────────────────────────────

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice, Bob
│   • admins (8KwNqY...)
│     Members: Alice
└─────────────────────────────────────────────────────────

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice, Bob
│   • admins (8KwNqY...)
│     Members: Alice
└─────────────────────────────────────────────────────────

[5] switch Bob
✓ Switched to peer: Bob

[6] send --message "Hello from Bob!"
✓ Sent message to #general: msg_yH4nVw...

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (1):
│       [5100ms] Bob: Hello from Bob!
└─────────────────────────────────────────────────────────

[7] switch Alice
✓ Switched to peer: Alice

[8] send --message "Hello from Alice!"
✓ Sent message to #general: msg_pQ2rXs...

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (1):
│       [5000ms] Alice: Hello from Alice!
└─────────────────────────────────────────────────────────

[9] sync --rounds 20
✓ Synced 20 rounds (t=7000ms -> 11000ms)

┌─────────────────────────────────────────────────────────
│ ALL PEERS STATE AFTER SYNC
└─────────────────────────────────────────────────────────

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (2):
│       [5100ms] Bob: Hello from Bob!
│       [5000ms] Alice: Hello from Alice!
└─────────────────────────────────────────────────────────

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (2):
│       [5100ms] Bob: Hello from Bob!
│       [5000ms] Alice: Hello from Alice!
└─────────────────────────────────────────────────────────

[10] show-all

┌═════════════════════════════════════════════════════════
│ COMPLETE SYSTEM STATE
├═════════════════════════════════════════════════════════
│ SESSION: 2 peers, 1 network
└═════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────
│ PEER: Alice (gBQNZX...)
│ USER: Alice (nF7kRm...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice, Bob
│   • admins (8KwNqY...)
│     Members: Alice
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (2):
│       [5100ms] Bob: Hello from Bob!
│       [5000ms] Alice: Hello from Alice!
├─────────────────────────────────────────────────────────
│ INVITES (1):
│   • invite_vR9sLp... (created by Alice)
└─────────────────────────────────────────────────────────

┌─────────────────────────────────────────────────────────
│ PEER: Bob (tD5mNk...)
│ USER: Bob (wP8qLx...)
│ NETWORK: uA2vWy...
├─────────────────────────────────────────────────────────
│ GROUPS (2):
│   • all_users (3DpXzT...)
│     Members: Alice, Bob
│   • admins (8KwNqY...)
│     Members: Alice
├─────────────────────────────────────────────────────────
│ CHANNELS (1):
│   • #general (hJ6tPm...)
│     Messages (2):
│       [5100ms] Bob: Hello from Bob!
│       [5000ms] Alice: Hello from Alice!
├─────────────────────────────────────────────────────────
│ INVITES (1):
│   • invite_vR9sLp... (created by Alice)
└─────────────────────────────────────────────────────────
```

## Command Reference

### Network & Peer Management
- `new-network --name <name>` - Create a new network and become first peer
- `new-peer --name <name> --invite <invite_link>` - Create peer and join existing network
- `switch <peer_name|peer_id>` - Switch active peer
- `list-peers` - List all peers in session

### Invites
- `create-invite` - Create network invite (requires admin)
- `list-invites` - List all invites visible to active peer

### Groups & Members
- `create-group --name <name>` - Create a new group
- `add-member --group <group_name|group_id> --user <user_name|user_id>` - Add user to group
- `list-groups` - List all groups visible to active peer
- `list-members --group <group_name|group_id>` - List members of a group

### Channels & Messages
- `create-channel --name <name> --group <group_name|group_id>` - Create a channel
- `send --channel <channel_name|channel_id> --message <text>` - Send a message
- `list-messages --channel <channel_name|channel_id>` - List messages in a channel

### Multi-Device (Linking)
- `create-link-invite` - Create a link invite for same user
- `link-device --link <link_url>` - Link a new device to existing user

### Sync & State
- `sync --rounds <n>` - Run n sync rounds (calls `tick.tick()` n times)
- `show` - Show current peer's state
- `show-all` - Show all peers' states
- `time` - Show current simulation time

### System
- `quit` / `exit` - Exit CLI (interactive mode)
- `help [command]` - Show help

## Example Flows from Scenario Tests

### 1. One Player Messaging (test_one_player_messaging.py)

```bash
# Non-interactive mode
./cli.py \
  --new-network Alice \
  --send "Hello" \
  --send "World" \
  --create-channel random \
  --send --channel random "Random thoughts" \
  --list-messages general \
  --list-messages random \
  --show-all
```

Interactive mode:
```
> new-network --name Alice
> send --message "Hello"
> send --message "World"
> create-channel --name random
> send --channel random --message "Random thoughts"
> list-messages --channel general
> list-messages --channel random
> show-all
```

### 2. Three Player Messaging (test_three_player_messaging.py)

```bash
# Non-interactive mode
./cli.py \
  --new-network Alice \
  --create-invite \
  --new-peer Bob --join-with-last-invite \
  --new-network Charlie \
  --sync 15 \
  --switch Bob --send "Hello from Bob!" \
  --switch Alice --send "Hello from Alice!" \
  --switch Charlie --send "Hello from Charlie!" \
  --sync 20 \
  --show-all
```

### 3. Link Device (test_link_device_new_groups.py)

```bash
# Non-interactive mode
./cli.py \
  --new-network Alice \
  --create-group "Group A" \
  --add-member --group "Group A" --user Alice \
  --sync 5 \
  --create-link-invite \
  --create-group "Group B" \
  --add-member --group "Group B" --user Alice \
  --sync 5 \
  --link-device --with-last-link \
  --sync 40 \
  --show-all
```

### 4. Admin Group (test_admin_group.py)

```bash
# Non-interactive mode
./cli.py \
  --new-network Alice \
  --create-invite \
  --new-peer Bob --join-with-last-invite \
  --sync 10 \
  --switch Alice \
  --add-member --group admins --user Bob \
  --sync 10 \
  --switch Bob \
  --create-invite \
  --new-peer Charlie --join-with-last-invite \
  --sync 80 \
  --show-all
```

## Implementation Architecture

### Session State Management
```python
class CLISession:
    """Manages the CLI session state across all peers."""

    def __init__(self):
        self.db = None  # Shared in-memory database
        self.peers = {}  # peer_id -> PeerContext
        self.active_peer_id = None
        self.current_time_ms = 0
        self.last_invite_link = None  # For --join-with-last-invite convenience
        self.last_link_url = None     # For --with-last-link convenience
```

### Peer Context
```python
class PeerContext:
    """Represents a peer's context in the session."""

    def __init__(self, peer_id, peer_shared_id, name):
        self.peer_id = peer_id
        self.peer_shared_id = peer_shared_id
        self.name = name
        self.user_id = None        # Set after network join
        self.network_id = None
        self.all_users_group_id = None
        self.admins_group_id = None
```

### State Display Functions
All state display must use ONLY the query functions from event modules:
- `channel.list_channels(peer_id, db)`
- `message.list_messages(channel_id, peer_id, db)`
- `group.list_all_groups(peer_id, db)`
- `group_member.list_members(group_id, peer_id, db)`
- `invite.list_invites(peer_id, db)` (if it exists)
- `network.get_all_users_group_id(network_id, peer_id, db)`
- `network.get_admin_group_id(network_id, peer_id, db)`

### Command Implementation Pattern
Each command must:
1. Call appropriate event function(s)
2. Update session state (e.g., last_invite_link)
3. Update current time if needed
4. Display result message
5. Display updated state

Example:
```python
def cmd_send_message(session, channel_id, content):
    """Send a message to a channel."""
    peer_ctx = session.get_active_peer()

    # 1. Call event function
    result = message.create(
        peer_id=peer_ctx.peer_id,
        channel_id=channel_id,
        content=content,
        t_ms=session.current_time_ms,
        db=session.db
    )

    # 2. Update session state
    session.current_time_ms += 100  # Increment time
    session.db.commit()

    # 3. Display result
    print(f"✓ Sent message: {result['id'][:10]}...")

    # 4. Display updated state
    display_peer_state(session, peer_ctx)
```

## Output Format Details

### State Display Hierarchy
```
PEER INFO
├─ GROUPS
│  └─ Members
├─ CHANNELS
│  └─ Messages
└─ INVITES
```

### Truncation Rules
- IDs: Show first 6-8 characters + "..."
- Long lists: Show first 5 items + "... (N more)"
- Messages: Show last 10 messages per channel
- State changes: Only show changed sections (with flag `--full` to show all)

### Color Coding (Terminal)
- ✓ Success: Green
- ✗ Error: Red
- Peer names: Cyan
- Group names: Yellow
- Channel names: Blue (with # prefix)
- IDs: Gray/dim
- Active peer marker (*): Green bold

### Machine-Readable Output
Add `--json` flag to output state as JSON instead of formatted text:
```bash
./cli.py --new-network Alice --json
```

Output:
```json
{
  "command": "new-network",
  "success": true,
  "peer_id": "gBQNZX...",
  "user_id": "nF7kRm...",
  "network_id": "uA2vWy...",
  "state": {
    "peer": { ... },
    "groups": [ ... ],
    "channels": [ ... ],
    "invites": [ ... ]
  }
}
```

## Testing Strategy

### Scenario Test Equivalence
Each scenario test should have a corresponding CLI script:
- `test_one_player_messaging.py` → `examples/one_player_messaging.cli`
- `test_three_player_messaging.py` → `examples/three_player_messaging.cli`
- `test_admin_group.py` → `examples/admin_group.cli`
- etc.

Running the CLI script should produce equivalent final state to running the test.

### CLI Script Format
```
# examples/three_player_messaging.cli
# Three players: Alice creates network, Bob joins, Charlie separate

new-network --name Alice
create-invite
new-peer --name Bob --join-with-last-invite
new-network --name Charlie
sync --rounds 15
switch Bob
send --message "Hello from Bob!"
switch Alice
send --message "Hello from Alice!"
switch Charlie
send --message "Hello from Charlie!"
sync --rounds 20
show-all

# Assertions (checked automatically)
assert peer Alice sees message "Hello from Bob!"
assert peer Alice sees message "Hello from Alice!"
assert peer Alice does-not-see message "Hello from Charlie!"
assert peer Bob sees message "Hello from Bob!"
assert peer Bob sees message "Hello from Alice!"
assert peer Bob does-not-see message "Hello from Charlie!"
assert peer Charlie sees message "Hello from Charlie!"
assert peer Charlie does-not-see message "Hello from Alice!"
assert peer Charlie does-not-see message "Hello from Bob!"
```

### Running CLI Tests
```bash
# Run a single CLI script
./cli.py --script examples/three_player_messaging.cli

# Run all CLI scripts as tests
./cli.py --test-all

# Compare CLI output to scenario test
./cli.py --compare test_three_player_messaging
```

## Future Enhancements

### Phase 2: Advanced Features
- File attachments: `attach --channel <channel> --file <path>`
- Message deletion: `delete-message <message_id>`
- User removal: `remove-user --user <user_id>`
- Forward secrecy: `rotate-key --group <group_id>`
- Pause/resume file transfer

### Phase 3: Debugging & Inspection
- Event log: `show-events --peer <peer_id> --type <type>`
- Sync details: `sync --rounds 10 --verbose`
- Key inspection: `show-keys --peer <peer_id>`
- Network graph: `show-network-graph`

### Phase 4: Performance Testing
- Benchmarking: `bench --scenario <scenario> --peers <n>`
- Profiling: `profile --scenario <scenario>`
- Load testing: `load-test --messages <n> --peers <n>`

## Next Steps

1. Review this prototype for completeness and clarity
2. Create implementation plan:
   - Core session management
   - Command parser (interactive + non-interactive)
   - State display engine
   - Command implementations (priority order)
   - Testing infrastructure
3. Implement core commands first:
   - `new-network`, `new-peer`, `switch`
   - `send`, `list-messages`
   - `sync`, `show`, `show-all`
4. Add remaining commands iteratively
5. Build testing framework
6. Create example CLI scripts for all scenario tests
