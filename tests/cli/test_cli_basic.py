"""
Basic CLI tests using non-interactive mode.

These tests pipe commands to cli.py via stdin and assert on output.
Written using TDD - tests written first, then code fixed to pass.
"""
import subprocess
import os
import re


CLI_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'cli.py')


def run_cli(commands: str) -> subprocess.CompletedProcess:
    """Run CLI with given commands and return result."""
    python_exe = "/home/hwilson/poc-6/venv/bin/python"
    return subprocess.run(
        [python_exe, CLI_PATH],
        input=commands.strip(),
        capture_output=True,
        text=True,
        cwd=os.path.dirname(CLI_PATH)
    )


def extract_main_section(output: str) -> str:
    """Extract the LAST MAIN section content from CLI output.

    Returns the content between the last "MAIN (#...)" and the next section or end of output.
    This ensures we're checking the final state after all commands have been executed.
    """
    matches = list(re.finditer(r'MAIN \([^)]*\):\n(.*?)(?=\n>|\nACCOUNTS:|\nINVITES:|\Z)', output, re.DOTALL))
    return matches[-1].group(1) if matches else ""


def test_single_user_messaging():
    """Alice creates network and sends message to herself."""
    commands = """
new-network --name "Alice's Network" --username alice --devicename desktop
send hello world
show-ui
"""
    result = run_cli(commands)

    # Should succeed
    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Should show account
    assert "alice (desktop)" in result.stdout

    # Should show message in MAIN section (not just in command echo)
    main_section = extract_main_section(result.stdout)
    assert "hello world" in main_section, \
        f"Message 'hello world' not found in MAIN section. MAIN content: {repr(main_section)}"

    # Should have success indicator
    assert "✓" in result.stdout


def test_two_user_messaging():
    """Alice invites Bob, both send messages and sync."""
    commands = """
new-network --name "Alice's Network" --username alice --devicename desktop
create-invite
join --username bob --devicename phone --invite 1
switch 1
send hello from alice
switch 2
send hi from bob
tick 10
switch 1
show-ui
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # After sync, alice should see both messages in MAIN section (not just in command echoes)
    main_section = extract_main_section(result.stdout)
    assert "hello from alice" in main_section, \
        f"Message 'hello from alice' not found in MAIN section. MAIN content: {repr(main_section)}"
    assert "hi from bob" in main_section, \
        f"Message 'hi from bob' not found in MAIN section. MAIN content: {repr(main_section)}"


def test_usernames_display_correctly():
    """Test that usernames from other accounts display correctly."""
    commands = """
new-network --name "Alice's Network" --username alice --devicename desktop
create-invite
join --username bob --devicename phone --invite 1
show-ui
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Both alice and bob should appear in the accounts section with their full names
    assert "alice (desktop)" in result.stdout, "alice account should be displayed"
    assert "bob (phone)" in result.stdout, "bob account should be displayed with correct username"

    # The account usernames should be readable, not "???"
    # Note: network_id might be "???" for bob due to backend sync issue, but that's separate
    assert "user_" in result.stdout, "Should show user IDs"
    assert "peer_" in result.stdout, "Should show peer IDs"


def test_list_commands():
    """Test list-accounts, list-channels, list-users, and time commands."""
    commands = """
new-network --name "Alice's Network" --username alice --devicename desktop
create-channel testing
list-accounts
list-channels
list-users
time
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    output = result.stdout

    # Find list-accounts output (should come after "> list-accounts")
    assert "> list-accounts" in output, "list-accounts command not found in output"
    # Should show alice with full account name format
    assert "alice (desktop)" in output, "alice account not shown in list-accounts"

    # Find list-channels output (should come after "> list-channels")
    assert "> list-channels" in output, "list-channels command not found in output"
    # Should show both channels
    lines_after_list_channels = output.split("> list-channels")[1].split(">")[0]
    assert "general" in lines_after_list_channels, "general channel not shown in list-channels"
    assert "testing" in lines_after_list_channels, "testing channel not shown in list-channels"

    # Find list-users output (should come after "> list-users")
    assert "> list-users" in output, "list-users command not found in output"
    # Should show alice as a user
    lines_after_list_users = output.split("> list-users")[1].split(">")[0]
    assert "alice" in lines_after_list_users, "alice not shown in list-users"

    # Find time output (should come after "> time")
    assert "> time" in output, "time command not found in output"
    # Should show time in milliseconds
    lines_after_time = output.split("> time")[1].split(">")[0].strip()
    assert "ms" in lines_after_time, "time output should contain 'ms'"


def test_create_channel_and_send():
    """Test creating a channel and sending messages to it."""
    commands = """
new-network --name "Alice's Network" --username alice --devicename desktop
create-channel random
select-channel 2
send test message
show-ui
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Should show the new channel
    assert "random" in result.stdout

    # Should show the message in MAIN section (not just in command echo)
    main_section = extract_main_section(result.stdout)
    assert "test message" in main_section, \
        f"Message 'test message' not found in MAIN section. MAIN content: {repr(main_section)}"


def test_auto_tick_behavior():
    """Test that auto-tick happens after send command."""
    commands = """
new-network --name "Alice's Network" --username alice --devicename desktop
create-invite
join --username bob --devicename phone --invite 1
switch 1
send from alice
switch 2
send from bob
switch 1
show-ui
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Auto-tick should sync messages (default 100 ticks)
    # After switching back to alice and showing, both messages should appear in MAIN section
    main_section = extract_main_section(result.stdout)
    assert "from alice" in main_section, \
        f"Message 'from alice' not found in MAIN section. MAIN content: {repr(main_section)}"
    assert "from bob" in main_section, \
        f"Message 'from bob' not found in MAIN section. MAIN content: {repr(main_section)}"

    # Should see auto-tick indicator
    assert "auto-syncing" in result.stdout or "⟳" in result.stdout


def test_link_device_basic():
    """Test linking a second device to an existing user."""
    commands = """
new-network --name "Test Network" --username alice --devicename desktop
create-link-invite
link-device --devicename laptop --invite 1
list-accounts
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Should have two accounts for alice
    assert "alice (desktop)" in result.stdout, "alice desktop should be displayed"
    assert "alice (laptop)" in result.stdout, "alice laptop should be displayed"

    # Both should have the same user_id prefix (they're the same user)
    # Extract user IDs from output
    import re
    user_ids = re.findall(r'user_([a-zA-Z0-9]+)', result.stdout)
    # Should have multiple references to same user_id for alice's devices
    assert len(user_ids) >= 2, "Should have multiple user_id references"

    # Success message should show correct user
    assert "linked device to existing user: alice" in result.stdout


def test_link_device_with_messaging():
    """Test that linked devices can send/receive messages."""
    commands = """
new-network --name "Test Network" --username alice --devicename desktop
create-link-invite
link-device --devicename laptop --invite 1
switch 1
send Hello from desktop
switch 2
send Hello from laptop
sync --ticks 50
switch 1
show
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Both messages should appear in MAIN section
    main_section = extract_main_section(result.stdout)
    assert "Hello from desktop" in main_section, \
        f"Desktop message not found. MAIN: {repr(main_section)}"
    assert "Hello from laptop" in main_section, \
        f"Laptop message not found. MAIN: {repr(main_section)}"


def test_join_and_link_device_together():
    """Test both join (new user) and link-device (existing user) in same session."""
    commands = """
new-network --name "Test Network" --username alice --devicename desktop
create-invite
join --username bob --devicename phone --invite 1
switch 1
create-link-invite
link-device --devicename laptop --invite 2
list-accounts
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Should have 3 accounts
    assert "alice (desktop)" in result.stdout
    assert "bob (phone)" in result.stdout
    assert "alice (laptop)" in result.stdout

    # Extract user IDs - alice's devices should share user_id
    # The accounts display shows user_XXXXX format
    lines = result.stdout.split('\n')
    alice_desktop_user = None
    alice_laptop_user = None
    bob_user = None

    for line in lines:
        if "alice (desktop)" in line:
            match = re.search(r'user_([a-zA-Z0-9]+)', line)
            if match:
                alice_desktop_user = match.group(1)
        elif "alice (laptop)" in line:
            match = re.search(r'user_([a-zA-Z0-9]+)', line)
            if match:
                alice_laptop_user = match.group(1)
        elif "bob (phone)" in line:
            match = re.search(r'user_([a-zA-Z0-9]+)', line)
            if match:
                bob_user = match.group(1)

    # Alice's devices should have same user_id
    assert alice_desktop_user == alice_laptop_user, \
        f"Alice's devices should share user_id: desktop={alice_desktop_user}, laptop={alice_laptop_user}"

    # Bob should have different user_id
    assert bob_user != alice_desktop_user, \
        f"Bob should have different user_id than Alice: bob={bob_user}, alice={alice_desktop_user}"


def test_link_device_wrong_invite_type():
    """Test that link-device rejects network join invites."""
    commands = """
new-network --name "Test Network" --username alice --devicename desktop
create-invite
link-device --devicename laptop --invite 1
"""
    result = run_cli(commands)

    # Should fail gracefully with helpful message
    assert "not a device linking invite" in result.stdout.lower() or "quiet://invite" in result.stdout


def test_create_link_invite_hint():
    """Test that create-link-invite shows correct usage hint."""
    commands = """
new-network --name "Test Network" --username alice --devicename desktop
create-link-invite
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Should show success and hint
    assert "created device link invite" in result.stdout
    assert "link-device --devicename" in result.stdout


def test_non_admin_can_create_link_invite():
    """Test that non-admin users can create link invites for themselves."""
    commands = """
new-network --name "Test Network" --username alice --devicename desktop
create-invite
join --username bob --devicename phone --invite 1
switch 2
create-link-invite
link-device --devicename tablet --invite 2
list-accounts
"""
    result = run_cli(commands)

    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Bob (non-admin) should be able to create a link invite
    assert "created device link invite" in result.stdout, \
        "Non-admin should be able to create link invite for themselves"

    # Bob should now have two devices
    assert "bob (phone)" in result.stdout, "bob phone should be displayed"
    assert "bob (tablet)" in result.stdout, "bob tablet should be displayed"

    # Success message should show bob
    assert "linked device to existing user: bob" in result.stdout


def test_help_text_matches_dispatcher():
    """Test that all commands in help text are wired up, and all wired commands are in help."""
    import sys
    import re as regex  # Use different name to avoid conflict with later re import
    sys.path.insert(0, os.path.dirname(CLI_PATH))

    # Get help text
    result = run_cli("help\nquit")
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    help_output = result.stdout

    # Extract just the help section (between "> help" and "> quit")
    help_section = help_output.split('> help')[1].split('> quit')[0] if '> help' in help_output else help_output

    # Extract commands from help text (lines that start with spaces and contain a command)
    help_commands = set()
    for line in help_section.split('\n'):
        line = line.strip()
        if not line or line.startswith('available') or line.startswith('('):
            continue
        # Skip lines that end with : (section headers)
        if line.endswith(':'):
            continue
        # Extract the command name (first word before any arguments)
        parts = line.split()
        if parts:
            cmd = parts[0]
            # Skip section headers and descriptions
            if cmd in ('Network', 'Joining/linking:', 'Account', 'Channels:', 'Messaging:',
                       'Admin:', 'Keys/sync:', 'Other:', 'Create', 'Join', 'Link'):
                continue
            # Must look like a command (lowercase with optional hyphens)
            if not regex.match(r'^[a-z][a-z-]*$', cmd):
                continue
            help_commands.add(cmd)

    # Read cli.py to extract dispatcher commands
    with open(CLI_PATH, 'r') as f:
        cli_source = f.read()

    # Find all 'elif cmd == "xxx"' patterns (including 'or cmd == "xxx"' for aliases)
    dispatcher_commands = set()
    for match in regex.finditer(r'(?:elif |or )cmd == ["\']([^"\']+)["\']', cli_source):
        cmd = match.group(1)
        dispatcher_commands.add(cmd)

    # Also add 'quit' and 'exit' which use different pattern (if cmd == "quit" or cmd == "exit")
    dispatcher_commands.add('quit')
    dispatcher_commands.add('exit')

    # These are intentionally not in help:
    # - exit: alias for quit
    # - help: meta-command (you discover it by typing 'help' anyway)
    internal_commands = {'exit', 'help'}
    dispatcher_commands -= internal_commands

    # Check that all help commands exist in dispatcher
    missing_from_dispatcher = help_commands - dispatcher_commands - {'setup:', 'management:'}
    assert not missing_from_dispatcher, \
        f"Commands in help but not wired up in dispatcher: {missing_from_dispatcher}"

    # Check that all dispatcher commands exist in help (except internal ones)
    missing_from_help = dispatcher_commands - help_commands
    assert not missing_from_help, \
        f"Commands in dispatcher but not in help text: {missing_from_help}"


def test_all_help_commands_execute_without_crash():
    """Test that every command in help can be executed without crashing (may error but shouldn't crash)."""
    # Commands that need setup first
    commands_needing_setup = [
        'send', 'select-channel', 'create-channel', 'list-channels', 'list-messages',
        'delete-message', 'edit-message', 'add-reaction', 'remove-reaction', 'list-reactions',
        'create-invite', 'create-link-invite', 'keys', 'show-group-keys', 'purge-keys',
        'remove-user', 'set-disappearing', 'show-ui'
    ]

    # Commands with their required minimal arguments
    command_tests = [
        # These work without any setup
        ('help', None),
        ('time', None),
        ('quit', None),

        # These need a network first but can be tested with just setup
        ('new-network --name Test --username alice --devicename desktop', None),
        ('list-accounts', None),
        ('list-users', None),
        ('switch 1', None),
        ('tick 1', None),
        ('set-auto-tick 10', None),
        ('fast-forward --days 1', None),
        ('show-ui', None),
        ('show', None),
        ('keys', None),
        ('keys --summary', None),
        ('show-group-keys', None),
        ('purge-keys', None),
        ('list-channels', None),
        ('create-channel testchan', None),
        ('select-channel 1', None),
        ('send "test message"', None),
        ('list-messages', None),
        ('edit-message 1 "edited"', None),
        ('delete-message 1', 'not found'),  # Will fail gracefully after edit
        ('add-reaction 1 thumbsup', 'not found'),  # Message was deleted
        ('remove-reaction 1 thumbsup', 'not found'),
        ('list-reactions 1', 'not found'),
        ('set-disappearing --days 1', None),
        ('set-disappearing --off', None),
        ('create-invite', None),
        ('join --username bob --devicename phone --invite 1', None),
        ('create-link-invite', None),
        ('link-device --devicename tablet --invite 2', None),
        ('remove-user 2', None),  # Remove bob
    ]

    # Build command sequence
    commands = "new-network --name Test --username alice --devicename desktop\n"
    for cmd, expected_error in command_tests[1:]:  # Skip first new-network
        if cmd != 'new-network --name Test --username alice --devicename desktop':
            commands += cmd + "\n"
    commands += "quit\n"

    result = run_cli(commands)

    # Should not crash (return code 0)
    assert result.returncode == 0, f"CLI crashed: {result.stderr}\n\nOutput: {result.stdout}"

    # Should not have Python exceptions
    assert "Traceback" not in result.stderr, f"Python exception occurred: {result.stderr}"
    assert "Traceback" not in result.stdout, f"Python exception in stdout: {result.stdout}"