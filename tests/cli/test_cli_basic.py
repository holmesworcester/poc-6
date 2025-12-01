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
show
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
sync --ticks 10
switch 1
show
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
show
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
show
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
show
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