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
    """Extract the MAIN section content from CLI output.

    Returns the content between "MAIN (#...)" and the next section or end of output.
    This ensures we're checking if messages appear in the actual display, not in
    command echoes or other parts of the output.
    """
    match = re.search(r'MAIN \([^)]*\):\n(.*?)(?=\n>|\nACCOUNTS:|\nINVITES:|\Z)', output, re.DOTALL)
    return match.group(1) if match else ""


def test_single_user_messaging():
    """Alice creates network and sends message to herself."""
    commands = """
new-network --name alice --device desktop
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
new-network --name alice --device desktop
create-invite
new-peer --name bob --device phone --invite 1
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
new-network --name alice --device desktop
create-invite
new-peer --name bob --device phone --invite 1
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
new-network --name alice --device desktop
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
new-network --name alice --device desktop
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
new-network --name alice --device desktop
create-invite
new-peer --name bob --device phone --invite 1
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
