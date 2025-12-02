"""
CLI tests for disappearing messages - presentation only.

These tests verify the CLI displays disappearing messages features correctly.
Logic is tested in scenario_tests/test_disappearing_messages_*.py
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


def test_set_disappearing_output_format():
    """Verify disappear shows correct confirmation message."""
    commands = """
new-network --name "Test" --username alice --device desktop
new-channel test-channel
channel 1
disappear --days 7
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    # Channel 1 is test-channel (most recently created, ordered by created_at DESC)
    assert "Set disappearing messages to 7d for #test-channel (affects new messages only)" in result.stdout


def test_set_disappearing_hours():
    """Verify disappear works with hours (via --time in ms)."""
    commands = """
new-network --name "Test" --username alice --device desktop
new-channel test-channel
channel 1
disappear --time 3600000
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    # Channel 1 is test-channel (most recently created)
    assert "Set disappearing messages to 1h for #test-channel (affects new messages only)" in result.stdout


def test_set_disappearing_off_output_format():
    """Verify turning off shows correct message."""
    commands = """
new-network --name "Test" --username alice --device desktop
new-channel test-channel
channel 1
disappear --days 7
disappear --off
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    # Channel 1 is test-channel (most recently created)
    assert "Turned off disappearing messages for #test-channel (affects new messages only)" in result.stdout


def test_set_disappearing_on_auto_selected_channel():
    """Verify disappear works on auto-selected #general channel."""
    # After new-network, #general is auto-selected
    commands = """
new-network --name "Test" --username alice --device desktop
disappear --days 7
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    # Should work on auto-selected #general
    assert "Set disappearing messages to 7d for #general (affects new messages only)" in result.stdout


def test_channel_list_shows_disappearing():
    """Verify channel list displays (disappearing: Xd) format."""
    commands = """
new-network --name "Test" --username alice --device desktop
new-channel ephemeral
channel 1
disappear --days 7
channels
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Find the channels output - channel 1 is ephemeral (most recently created)
    lines_after_list = result.stdout.split("> channels")[1].split(">")[0]
    assert "(disappearing: 7d)" in lines_after_list


def test_channel_list_no_disappearing_for_permanent():
    """Verify permanent channels don't show disappearing indicator."""
    commands = """
new-network --name "Test" --username alice --device desktop
channels
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"

    # Find the channels output
    lines_after_list = result.stdout.split("> channels")[1].split(">")[0]
    # #general should NOT have disappearing indicator
    assert "#general" in lines_after_list
    # Should not have (disappearing: ...) for general
    general_line = [l for l in lines_after_list.split('\n') if 'general' in l][0]
    assert "(disappearing:" not in general_line


def test_message_shows_expires_in():
    """Verify message display includes (expires in: Xd Yh) format."""
    commands = """
new-network --name "Test" --username alice --device desktop
new-channel ephemeral
channel 2
disappear --days 7
send Test message
show
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    assert "expires in:" in result.stdout


def test_fast_forward_output_format():
    """Verify fast-forward shows time and displays state."""
    commands = """
new-network --name "Test" --username alice --device desktop
fast-forward --days 3
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    assert "Fast-forwarded 3 days" in result.stdout
    assert "current time:" in result.stdout
    # Should also show state after fast-forward
    assert "ACCOUNTS:" in result.stdout


def test_fast_forward_singular_day():
    """Verify fast-forward uses singular 'day' for 1 day."""
    commands = """
new-network --name "Test" --username alice --device desktop
fast-forward --days 1
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    assert "Fast-forwarded 1 day" in result.stdout
    assert "Fast-forwarded 1 days" not in result.stdout


def test_help_shows_new_commands():
    """Verify help includes disappear and fast-forward."""
    commands = """
help
"""
    result = run_cli(commands)
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    assert "disappear" in result.stdout
    assert "fast-forward" in result.stdout
