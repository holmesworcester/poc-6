"""
CLI validation regression tests.

These tests verify that the CLI properly validates input and rejects:
- Empty or whitespace-only messages, channel names, and reactions
- Negative fast-forward days
- And that fast-forward properly refreshes expired state (prekeys)
"""
import subprocess
import os


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


class TestMessageValidation:
    """Test message content validation."""

    def test_empty_message_rejected(self):
        """Empty message should be rejected."""
        commands = """
new-network --name Test --username alice --devicename desktop
send ""
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "message cannot be empty or whitespace-only" in result.stdout

    def test_whitespace_only_message_rejected(self):
        """Whitespace-only message should be rejected."""
        commands = """
new-network --name Test --username alice --devicename desktop
send "   "
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "message cannot be empty or whitespace-only" in result.stdout

    def test_valid_message_accepted(self):
        """Valid message with content should be accepted."""
        commands = """
new-network --name Test --username alice --devicename desktop
send "hello world"
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "✓ sent message" in result.stdout
        assert "hello world" in result.stdout


class TestChannelNameValidation:
    """Test channel name validation."""

    def test_empty_channel_name_rejected(self):
        """Empty channel name should be rejected."""
        commands = """
new-network --name Test --username alice --devicename desktop
new-channel ""
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "channel name cannot be empty or whitespace-only" in result.stdout

    def test_whitespace_only_channel_name_rejected(self):
        """Whitespace-only channel name should be rejected."""
        commands = """
new-network --name Test --username alice --devicename desktop
new-channel "   "
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "channel name cannot be empty or whitespace-only" in result.stdout

    def test_valid_channel_name_accepted(self):
        """Valid channel name should be accepted."""
        commands = """
new-network --name Test --username alice --devicename desktop
new-channel random
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "✓ created channel #random" in result.stdout

    def test_duplicate_channel_names_allowed(self):
        """Duplicate channel names are allowed by design (ID-based selection)."""
        commands = """
new-network --name Test --username alice --devicename desktop
new-channel duplicate
new-channel duplicate
channels
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        # Should have two channels named "duplicate" (plus general)
        assert result.stdout.count("#duplicate") >= 2


class TestReactionValidation:
    """Test reaction (emoji) validation."""

    def test_empty_reaction_rejected(self):
        """Empty reaction should be rejected."""
        commands = """
new-network --name Test --username alice --devicename desktop
send "test message"
react 1 ""
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "reaction cannot be empty or whitespace-only" in result.stdout

    def test_whitespace_only_reaction_rejected(self):
        """Whitespace-only reaction should be rejected."""
        commands = """
new-network --name Test --username alice --devicename desktop
send "test message"
react 1 "   "
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "reaction cannot be empty or whitespace-only" in result.stdout

    def test_valid_reaction_accepted(self):
        """Valid reaction should be accepted."""
        commands = """
new-network --name Test --username alice --devicename desktop
send "test message"
react 1 "👍"
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "✓ added reaction 👍" in result.stdout


class TestFastForwardValidation:
    """Test fast-forward command validation."""

    def test_negative_days_rejected(self):
        """Negative days should be rejected."""
        commands = """
new-network --name Test --username alice --devicename desktop
fast-forward --days -1
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "days must be non-negative" in result.stdout

    def test_zero_days_allowed(self):
        """Zero days should be allowed (no-op)."""
        commands = """
new-network --name Test --username alice --devicename desktop
fast-forward --days 0
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "Fast-forwarded 0 days" in result.stdout

    def test_positive_days_allowed(self):
        """Positive days should be allowed."""
        commands = """
new-network --name Test --username alice --devicename desktop
fast-forward --days 1
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "Fast-forwarded 1 day" in result.stdout


class TestFastForwardPrekeyRefresh:
    """Test that fast-forward properly handles prekey expiration."""

    def test_invite_works_after_moderate_fast_forward(self):
        """After moderate fast-forward, invites should still work (tick refreshes prekeys)."""
        commands = """
new-network --name Test --username alice --devicename desktop
fast-forward --days 7
invite
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        # Should either succeed or have a clear error, not crash
        # With tick running after fast-forward, prekeys should be refreshed
        assert "Traceback" not in result.stdout
        assert "Traceback" not in result.stderr

    def test_fast_forward_runs_tick(self):
        """Fast-forward should run tick to handle time-dependent state."""
        # This is an indirect test - we verify no crashes occur
        # and that operations after fast-forward work
        commands = """
new-network --name Test --username alice --devicename desktop
auto-tick 0
fast-forward --days 30
keys --summary
quit
"""
        result = run_cli(commands)
        assert result.returncode == 0
        assert "Traceback" not in result.stdout
        assert "Traceback" not in result.stderr
        # Should show keys summary without crashing
        assert "KEYS" in result.stdout
