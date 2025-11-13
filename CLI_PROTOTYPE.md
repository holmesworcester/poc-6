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

### Auto-Tick After Commands
To simulate realistic message propagation, the CLI automatically runs sync ticks after commands that generate events:
- **Default: 100 ticks** after commands like `send`, `create-invite`, `add-admin`, etc.
- **Settable**: `set-auto-tick N` command to change the value during session
- **Disable**: `set-auto-tick 0` for manual control
- **Explicit sync**: `sync --ticks N` for additional manual ticks

This makes the CLI more realistic without requiring constant manual syncing. The default of 100 ticks should be enough for most scenarios to converge.

Example with auto-tick (default 100):
```
> send "Hello"
✓ Sent message
⟳ Auto-syncing 100 ticks...
✓ Synced (t=2000ms -> 12000ms)

MAIN (#general):
  [2000ms] Alice: Hello
```

Example changing auto-tick:
```
> set-auto-tick 20
✓ Auto-tick set to 20 ticks

> send "Hello"
✓ Sent message
⟳ Auto-syncing 20 ticks...
✓ Synced (t=2000ms -> 4000ms)
```

Example with manual control:
```
> set-auto-tick 0
✓ Auto-tick disabled

> send "Hello"
✓ Sent message (not synced)

> sync --ticks 50
✓ Synced 50 ticks
```

Note: Each tick represents one call to `tick.tick()`. With default 100ms per tick, 100 ticks = 10 seconds of simulation time.

### Slack-Like Interface
The UI mimics Slack's layout with three sections shown sequentially:
1. **Accounts** - List of all your accounts/devices (corresponds to peers internally)
2. **Sidebar** - List of users in the network (informational) + list of channels (selectable)
3. **Main** - List of messages in the selected channel

**What's hidden (under the hood):**
- Groups - used internally for permissions but not shown in UI
- Invites - only displayed at creation time as command output, not persisted in UI
- Admin status - not explicitly shown (but affects what commands are allowed)

The CLI maintains ephemeral in-memory state for which account and channel are currently selected.

**Note:** DMs are not yet supported - messages go to channels. Users list in sidebar is informational only (shows who's in the network).

### State Display Format
Simple bulleted/nested lists, no boxes or special formatting:

```
ACCOUNTS:
  * alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy
    alice (phone) - user_nF7kRm, peer_tD5mNk, network_uA2vWy
    bob (desktop) - user_wP8qLx, peer_hJ6tPm, network_uA2vWy
    charlie (desktop) - user_zQ9rMy, peer_kL8vRp, network_xyz789

SIDEBAR (alice - desktop):
  users:
    alice
    bob

  channels:
    * #general
      #random

MAIN (#general):
  [32000ms] alice: Hello from Alice!
  [22000ms] bob: Hello from Bob!
```

The `*` indicates the currently selected item.

Format notes:
- **Accounts list**: Shows full context - `username (device) - user_id, peer_id, network_id`
- **Sidebar users**: Informational list of users in the network (not selectable, no DMs yet)
- **Sidebar channels**: Selectable channels - where messages go
- **Main header**: Shows selected channel (e.g., "#general")
- **Messages**: Just show username (e.g., "alice:") - no device name
- All lowercase for usernames and device names
- IDs are truncated to first 6 characters for readability

## Interactive Mode

### Starting the CLI
```bash
$ ./cli.py --interactive
Welcome to POC-6 CLI (Interactive Mode)

ACCOUNTS:
  (none)

>
```

### Session Flow Example: Three Player Messaging

```
> new-network --name alice --device desktop
✓ created network as alice
✓ selected account: alice (desktop)
✓ selected channel: #general

ACCOUNTS:
  * alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy

SIDEBAR (alice - desktop):
  users:
    alice

  channels:
    * #general

MAIN (#general):
  (no messages)

> create-invite
✓ created invite: poc6://invite/eyJpbnZpdGVfaWQiOiJ2Ujlz...
⟳ auto-syncing 100 ticks...
✓ synced (t=2000ms -> 12000ms)

ACCOUNTS:
  * alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy

SIDEBAR (alice - desktop):
  users:
    alice

  channels:
    * #general

MAIN (#general):
  (no messages)

> new-account --name bob --device desktop --invite poc6://invite/eyJpbnZpdGVfaWQiOiJ2Ujlz...
✓ created account: bob (desktop)
✓ joined network: uA2vWy
✓ selected account: bob (desktop)
✓ selected channel: #general
⟳ auto-syncing 100 ticks...
✓ synced (t=12000ms -> 22000ms)

ACCOUNTS:
    alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy
  * bob (desktop) - user_wP8qLx, peer_hJ6tPm, network_uA2vWy

SIDEBAR (bob - desktop):
  users:
    alice
    bob

  channels:
    * #general

MAIN (#general):
  (no messages)

> send "Hello from Bob!"
✓ sent message
⟳ auto-syncing 100 ticks...
✓ synced (t=22000ms -> 32000ms)

ACCOUNTS:
    alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy
  * bob (desktop) - user_wP8qLx, peer_hJ6tPm, network_uA2vWy

SIDEBAR (bob - desktop):
  users:
    alice
    bob

  channels:
    * #general

MAIN (#general):
  [22000ms] bob: Hello from Bob!

> switch alice
✓ selected account: alice (desktop)

ACCOUNTS:
  * alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy
    bob (desktop) - user_wP8qLx, peer_hJ6tPm, network_uA2vWy

SIDEBAR (alice - desktop):
  users:
    alice
    bob

  channels:
    * #general

MAIN (#general):
  [22000ms] bob: Hello from Bob!

> send "Hello from Alice!"
✓ sent message
⟳ auto-syncing 100 ticks...
✓ synced (t=32000ms -> 42000ms)

ACCOUNTS:
  * alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy
    bob (desktop) - user_wP8qLx, peer_hJ6tPm, network_uA2vWy

SIDEBAR (alice - desktop):
  users:
    alice
    bob

  channels:
    * #general

MAIN (#general):
  [32000ms] alice: Hello from Alice!
  [22000ms] bob: Hello from Bob!

> switch bob
✓ selected account: bob (desktop)

ACCOUNTS:
    alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy
  * bob (desktop) - user_wP8qLx, peer_hJ6tPm, network_uA2vWy

SIDEBAR (bob - desktop):
  users:
    alice
    bob

  channels:
    * #general

MAIN (#general):
  [32000ms] alice: Hello from Alice!
  [22000ms] bob: Hello from Bob!

> list-accounts
ACCOUNTS:
  * bob (desktop) - user_wP8qLx, peer_hJ6tPm, network_uA2vWy
    alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy

> quit
goodbye!
```

## Non-Interactive Mode

### Command Syntax
Commands are provided as CLI arguments. State is shown after each command.

### Example: Three Player Messaging
```bash
$ ./cli.py \
  --new-network Alice \
  --create-invite \
  --new-account Bob --join-with-last-invite \
  --sync 15 \
  --switch Bob \
  --send "Hello from Bob!" \
  --switch Alice \
  --send "Hello from Alice!" \
  --sync 20 \
  --show-all

[1] new-network --name Alice
✓ Created network as Alice
✓ Selected account: Alice
✓ Selected channel: #general

ACCOUNTS:
  * Alice - alice, network_uA2vWy

SIDEBAR (Alice):
  * #general

MAIN (#general):
  (no messages)

[2] create-invite
✓ Created invite: poc6://invite/eyJpbnZpdGVfaWQiOiJ2Ujlz...

ACCOUNTS:
  * Alice - alice, network_uA2vWy

SIDEBAR (Alice):
  * #general

MAIN (#general):
  (no messages)

[3] new-account --name Bob --join-with-last-invite
✓ Created account: Bob
✓ Joined network: uA2vWy
✓ Selected account: Bob
✓ Selected channel: #general

ACCOUNTS:
    Alice - alice, network_uA2vWy
  * Bob - bob, network_uA2vWy

SIDEBAR (Bob):
  * #general

MAIN (#general):
  (no messages)

[4] sync --ticks 15
✓ Synced 15 ticks (t=4000ms -> 7000ms)

ACCOUNTS:
    Alice - alice, network_uA2vWy
  * Bob - bob, network_uA2vWy

SIDEBAR (Bob):
  * #general

MAIN (#general):
  (no messages)

[5] send "Hello from Bob!"
✓ Sent message

ACCOUNTS:
    Alice - alice, network_uA2vWy
  * Bob - bob, network_uA2vWy

SIDEBAR (Bob):
  * #general

MAIN (#general):
  [5100ms] Bob: Hello from Bob!

[6] switch Alice
✓ Selected account: Alice

ACCOUNTS:
  * Alice - alice, network_uA2vWy
    Bob - bob, network_uA2vWy

SIDEBAR (Alice):
  * #general

MAIN (#general):
  (no messages yet - needs sync)

[7] send "Hello from Alice!"
✓ Sent message

ACCOUNTS:
  * Alice - alice, network_uA2vWy
    Bob - bob, network_uA2vWy

SIDEBAR (Alice):
  * #general

MAIN (#general):
  [5000ms] Alice: Hello from Alice!

[8] sync --ticks 20
✓ Synced 20 ticks (t=7000ms -> 11000ms)

ACCOUNTS:
  * Alice - alice, network_uA2vWy
    Bob - bob, network_uA2vWy

SIDEBAR (Alice):
  * #general

MAIN (#general):
  [5100ms] Bob: Hello from Bob!
  [5000ms] Alice: Hello from Alice!

[9] show-all

=== ALL ACCOUNTS ===

ACCOUNT: Alice
  Network: uA2vWy
  User ID: nF7kRm
  Channels: #general

  #general:
    [5100ms] Bob: Hello from Bob!
    [5000ms] Alice: Hello from Alice!

ACCOUNT: Bob
  Network: uA2vWy
  User ID: wP8qLx
  Channels: #general

  #general:
    [5100ms] Bob: Hello from Bob!
    [5000ms] Alice: Hello from Alice!
```

## Command Reference

### Network & Account Management
- `new-network --name <name> --device <device_name>` - Create a new network and first account
- `new-account --name <name> --device <device_name> --invite <invite_link>` - Create account and join network
- `switch <account_name>` - Switch to a different account (use full name with device, e.g., "Alice (Desktop)")
- `list-accounts` - List all accounts in session with details

Note: Device names distinguish multiple accounts for the same user (e.g., "Alice (Desktop)" vs "Alice (Phone)")

### Invites
- `create-invite` - Create network invite (requires admin) - shows invite link immediately

### Channels & Messages
- `create-channel --name <name>` - Create a new channel
- `select-channel <channel_name>` - Select a different channel
- `send <message>` - Send message to currently selected channel
- `list-channels` - List all channels in the current network
- `list-users` - List all users in the current network

### Multi-Device (Linking)
- `create-link-invite` - Create a link invite for same user
- `link-device --device <device_name> --link <link_url>` - Link a new device to existing user

Note: Linked devices share the same user_id but have different device names (e.g., "Alice (Desktop)" and "Alice (Phone)")

### Admin Operations
- `add-admin --user <user_name>` - Add user as admin (requires admin)
- `list-admins` - List admin users

### Sync & State
- `sync --ticks <n>` - Run n sync ticks (calls `tick.tick()` n times)
- `set-auto-tick <n>` - Set the number of auto-ticks after event commands (0 to disable)
- `show` - Show current state (accounts + sidebar + main)
- `time` - Show current simulation time

### System
- `quit` / `exit` - Exit CLI (interactive mode)
- `help [command]` - Show help

## State Display Sections

### ACCOUNTS Section
Shows all accounts in the session with selection indicator:
```
ACCOUNTS:
  * alice (desktop) - user_nF7kRm, peer_gBQNZX, network_uA2vWy
    alice (phone) - user_nF7kRm, peer_tD5mNk, network_uA2vWy
    bob (desktop) - user_wP8qLx, peer_hJ6tPm, network_uA2vWy
    charlie (desktop) - user_zQ9rMy, peer_kL8vRp, network_xyz789
```

Format: `[*] <username> (<device>) - user_<id>, peer_<id>, network_<id>`

The `*` indicates the currently selected account.

Notes:
- All lowercase for usernames and device names
- Shows full context: user_id, peer_id, and network_id
- Multiple accounts can share the same username but have different device names
- Accounts with the same user_id are linked devices for the same user
- IDs are truncated to first 6 characters

### SIDEBAR Section
Shows users in the network (informational) and channels (selectable):
```
SIDEBAR (alice - desktop):
  users:
    alice
    bob
    charlie

  channels:
    * #general
      #random
```

Format:
- users: `<username>` (informational, not selectable)
- channels: `[*] #<channel_name>` (selectable)

The `*` indicates the currently selected channel for messaging.

### MAIN Section
Shows messages in the selected channel:
```
MAIN (#general):
  [32000ms] alice: Hello from Alice!
  [22000ms] bob: Hello from Bob!
  [12000ms] alice: Welcome to the network!
```

Format:
- Header: `(#<channel_name>)`
- Message: `[<timestamp_ms>] <username>: <message_content>`

Messages are shown in reverse chronological order (newest first).

### Empty States
When sections are empty:
```
ACCOUNTS:
  (no accounts)

SIDEBAR (alice - desktop):
  users:
    (no users)

  channels:
    (no channels)

MAIN (#general):
  (no messages)
```

## Example Flows from Scenario Tests

### 1. One Player Messaging (test_one_player_messaging.py)

```bash
# Non-interactive mode
./cli.py \
  --new-network Alice \
  --send "Hello" \
  --send "World" \
  --create-channel random \
  --select-channel random \
  --send "Random thoughts" \
  --show-all
```

Interactive mode:
```
> new-network --name Alice
> send "Hello"
> send "World"
> create-channel --name random
> select-channel random
> send "Random thoughts"
> show-all
```

Expected output after all commands:
```
ACCOUNT: Alice
  Network: uA2vWy
  Channels: #general, #random

  #general:
    [3000ms] Alice: World
    [2000ms] Alice: Hello

  #random:
    [5000ms] Alice: Random thoughts
```

### 2. Three Player Messaging (test_three_player_messaging.py)

```bash
# Non-interactive mode
./cli.py \
  --new-network Alice \
  --create-invite \
  --new-account Bob --join-with-last-invite \
  --new-network Charlie \
  --sync 15 \
  --switch Bob --send "Hello from Bob!" \
  --switch Alice --send "Hello from Alice!" \
  --switch Charlie --send "Hello from Charlie!" \
  --sync 20 \
  --show-all
```

Expected final state:
```
ACCOUNT: Alice
  Network: uA2vWy (with Bob)
  #general:
    [5100ms] Bob: Hello from Bob!
    [5000ms] Alice: Hello from Alice!

ACCOUNT: Bob
  Network: uA2vWy (with Alice)
  #general:
    [5100ms] Bob: Hello from Bob!
    [5000ms] Alice: Hello from Alice!

ACCOUNT: Charlie
  Network: xyz789 (separate)
  #general:
    [5200ms] Charlie: Hello from Charlie!
```

### 3. Link Device (test_link_device_new_groups.py)

```bash
# Non-interactive mode
./cli.py \
  --new-network Alice \
  --create-channel "team-alpha" \
  --sync 5 \
  --create-link-invite \
  --create-channel "team-beta" \
  --sync 5 \
  --link-device --name "Alice-Phone" --with-last-link \
  --sync 40 \
  --show-all
```

Expected final state:
```
ACCOUNT: Alice (Desktop)
  User: alice_user_xyz
  Network: uA2vWy
  Channels: #general, #team-alpha, #team-beta

ACCOUNT: Alice-Phone (Linked Device)
  User: alice_user_xyz (same user!)
  Network: uA2vWy
  Channels: #general, #team-alpha, #team-beta
```

### 4. Admin Operations (test_admin_group.py)

```bash
# Non-interactive mode
./cli.py \
  --new-network Alice \
  --create-invite \
  --new-account Bob --join-with-last-invite \
  --sync 10 \
  --switch Alice \
  --add-admin Bob \
  --sync 10 \
  --switch Bob \
  --create-invite \
  --new-account Charlie --join-with-last-invite \
  --sync 80 \
  --show-all
```

Expected behavior:
- Alice creates network (becomes admin automatically)
- Bob joins (not admin initially)
- Alice adds Bob as admin
- Bob can now create invites
- Charlie joins via Bob's invite

## Implementation Architecture

### Session State Management
```python
class CLISession:
    """Manages the CLI session state."""

    def __init__(self):
        self.db = None  # Shared in-memory database
        self.accounts = {}  # account_name -> AccountContext
        self.selected_account = None  # Currently selected account name
        self.selected_channel = None  # Currently selected channel name
        self.current_time_ms = 0
        self.auto_tick_count = 100  # Number of auto-ticks after event commands (default 100)
        self.last_invite_link = None  # Internal only - for --join-with-last-invite convenience
        self.last_link_url = None     # Internal only - for --with-last-link convenience

    def get_selected_account(self):
        """Get the currently selected account context."""
        if not self.selected_account:
            raise ValueError("No account selected")
        return self.accounts[self.selected_account]
```

### Account Context
```python
class AccountContext:
    """Represents an account in the session.

    'Account' is the frontend term - internally corresponds to a peer.
    """

    def __init__(self, user_name, device_name, peer_id, peer_shared_id):
        self.user_name = user_name     # User's display name (e.g., "Alice")
        self.device_name = device_name # Device name (e.g., "Desktop", "Phone")
        self.peer_id = peer_id         # Backend peer ID
        self.peer_shared_id = peer_shared_id
        self.user_id = None            # Set after network join
        self.network_id = None         # Set after network join

    @property
    def full_name(self):
        """Full account name for display: 'Alice (Desktop)'"""
        return f"{self.user_name} ({self.device_name})"
```

### State Display Functions
All state display must use ONLY query functions from event modules:

```python
def display_state(session):
    """Display the three-section Slack-like state."""
    display_accounts(session)
    print()
    display_sidebar(session)
    print()
    display_main(session)

def display_accounts(session):
    """Display ACCOUNTS section."""
    print("ACCOUNTS:")
    if not session.accounts:
        print("  (no accounts)")
        return

    for full_name, account in session.accounts.items():
        selected = "*" if full_name == session.selected_account else " "
        short_user = account.user_id[:6] if account.user_id else "???"
        short_net = account.network_id[:6] if account.network_id else "???"
        print(f"  {selected} {account.full_name} - user_{short_user}, network_{short_net}")

def display_sidebar(session):
    """Display SIDEBAR section for selected account."""
    account = session.get_selected_account()
    print(f"SIDEBAR ({account.name}):")

    # Use event function to get channels
    from events.content import channel
    channels = channel.list_channels(account.peer_id, session.db)

    if not channels:
        print("  (no channels)")
        return

    for ch in channels:
        selected = "*" if ch['name'] == session.selected_channel else " "
        print(f"  {selected} #{ch['name']}")

def display_main(session):
    """Display MAIN section for selected channel."""
    account = session.get_selected_account()

    if not session.selected_channel:
        print("MAIN:")
        print("  (no channel selected)")
        return

    print(f"MAIN (#{session.selected_channel}):")

    # Find channel_id by name
    from events.content import channel, message
    channels = channel.list_channels(account.peer_id, session.db)
    channel_id = None
    for ch in channels:
        if ch['name'] == session.selected_channel:
            channel_id = ch['channel_id']
            break

    if not channel_id:
        print("  (channel not found)")
        return

    # Use event function to get messages
    messages = message.list_messages(channel_id, account.peer_id, session.db)

    if not messages:
        print("  (no messages)")
        return

    for msg in messages:
        # Get author name by looking up user_id
        author_name = get_user_name(msg['author_id'], session)
        print(f"  [{msg['created_at']}ms] {author_name}: {msg['content']}")
```

### Command Implementation Pattern
Each command must:
1. Call appropriate event function(s)
2. Update session state (selected account, channel, etc.)
3. Update current time if needed
4. Display result message
5. Display updated state

Example:
```python
def cmd_send_message(session, content):
    """Send a message to the selected channel."""
    account = session.get_selected_account()

    if not session.selected_channel:
        print("✗ No channel selected")
        return

    # Find channel_id
    from events.content import channel, message
    channels = channel.list_channels(account.peer_id, session.db)
    channel_id = None
    for ch in channels:
        if ch['name'] == session.selected_channel:
            channel_id = ch['channel_id']
            break

    if not channel_id:
        print(f"✗ Channel #{session.selected_channel} not found")
        return

    # Call event function
    result = message.create(
        peer_id=account.peer_id,
        channel_id=channel_id,
        content=content,
        t_ms=session.current_time_ms,
        db=session.db
    )

    # Update state
    session.current_time_ms += 100
    session.db.commit()

    # Display result
    print("✓ Sent message")
    print()

    # Display updated state
    display_state(session)
```

## Terminology Mapping

### Frontend (CLI UI) ↔ Backend (Code)
- **Account** ↔ Peer (peer_id, peer_shared_id)
- **User** ↔ User (user_id) - same term
- **Channel** ↔ Channel (channel_id) - same term
- **Message** ↔ Message (message_id) - same term

Groups are hidden from the UI but used internally for permissions.

## Output Format Details

### Simplicity
- Use simple bulleted lists with `*` for selection
- No box drawing characters or fancy formatting
- Easy to read, easy to parse

### Truncation Rules
- IDs: Show first 6 characters (e.g., `uA2vWy`, `nF7kRm`)
- Long messages: Truncate at 80 characters with "..."
- Message lists: Show last 20 messages (most recent first)

### Color Coding (Terminal)
- ✓ Success: Green
- ✗ Error: Red
- Account names: Cyan
- Channel names: Blue
- Selected indicator (*): Green/bold
- Timestamps: Gray/dim

### Machine-Readable Output
Add `--json` flag for JSON output:
```bash
./cli.py --new-network Alice --json
```

Output:
```json
{
  "command": "new-network",
  "success": true,
  "account": {
    "name": "Alice",
    "peer_id": "gBQNZX...",
    "user_id": "nF7kRm...",
    "network_id": "uA2vWy..."
  },
  "state": {
    "accounts": [...],
    "channels": [...],
    "messages": [...]
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

### CLI Script Format
```
# examples/three_player_messaging.cli
# Three players: Alice creates network, Bob joins, Charlie separate

new-network --name Alice
create-invite
new-account --name Bob --join-with-last-invite
new-network --name Charlie
sync --ticks 15
switch Bob
send "Hello from Bob!"
switch Alice
send "Hello from Alice!"
switch Charlie
send "Hello from Charlie!"
sync --ticks 20
show-all

# Assertions (checked automatically)
assert account Alice has message "Hello from Bob!" in #general
assert account Alice has message "Hello from Alice!" in #general
assert account Alice missing message "Hello from Charlie!" in #general
assert account Bob has message "Hello from Bob!" in #general
assert account Bob has message "Hello from Alice!" in #general
assert account Bob missing message "Hello from Charlie!" in #general
assert account Charlie has message "Hello from Charlie!" in #general
assert account Charlie missing message "Hello from Alice!" in #general
assert account Charlie missing message "Hello from Bob!" in #general
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
- File attachments
- Message deletion
- User removal
- Channel management (archive, etc.)

### Phase 3: Debugging & Inspection
- Event log viewer
- Sync details (verbose mode)
- Network graph visualization

### Phase 4: Performance Testing
- Benchmarking scenarios
- Profiling
- Load testing

## Next Steps

1. **Review this updated prototype** - Does the Slack-like UI work?
2. **Create implementation plan**:
   - Core session management (accounts, selection state)
   - Command parser (interactive + non-interactive)
   - State display engine (accounts, sidebar, main)
   - Command implementations (priority order)
   - Testing infrastructure
3. **Implement core commands**:
   - `new-network`, `new-account`, `switch`
   - `send`, `select-channel`
   - `sync`, `show`, `show-all`
4. **Add remaining commands iteratively**
5. **Build testing framework**
6. **Create example CLI scripts for all scenario tests**
