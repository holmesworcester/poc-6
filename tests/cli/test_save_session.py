"""
Tests for the save-session CLI command.

Tests cover:
- Basic save functionality
- File creation and content
- Replay of saved sessions
- Edge cases (no commands, invalid names)
"""
import subprocess
import os
import tempfile
import shutil


CLI_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'cli.py')
SAVED_SESSIONS_DIR = os.path.join(os.path.dirname(CLI_PATH), 'tests', 'saved-sessions')


def run_cli(commands: str, cwd: str = None) -> subprocess.CompletedProcess:
    """Run CLI with given commands and return result."""
    python_exe = "/home/hwilson/poc-6/venv/bin/python"
    return subprocess.run(
        [python_exe, CLI_PATH],
        input=commands.strip(),
        capture_output=True,
        text=True,
        cwd=cwd or os.path.dirname(CLI_PATH)
    )


def cleanup_session_file(name: str):
    """Remove a saved session file if it exists."""
    path = os.path.join(SAVED_SESSIONS_DIR, f"{name}.sh")
    if os.path.exists(path):
        os.remove(path)


class TestSaveSession:
    """Tests for save-session command."""

    def test_save_session_creates_file(self):
        """save-session creates a .sh file in saved-sessions directory."""
        session_name = "test_creates_file"
        cleanup_session_file(session_name)

        try:
            commands = f"""
new-network --name TestNet --username alice --devicename laptop
send hello
save-session {session_name}
"""
            result = run_cli(commands)
            assert result.returncode == 0, f"CLI failed: {result.stderr}"

            # File should exist
            session_path = os.path.join(SAVED_SESSIONS_DIR, f"{session_name}.sh")
            assert os.path.exists(session_path), f"Session file not created at {session_path}"

            # Should report success
            assert f"saved 2 commands" in result.stdout
        finally:
            cleanup_session_file(session_name)

    def test_save_session_file_content(self):
        """Saved session file contains correct commands."""
        session_name = "test_file_content"
        cleanup_session_file(session_name)

        try:
            commands = f"""
new-network --name TestNet --username alice --devicename laptop
send "hello world"
invite
save-session {session_name}
"""
            result = run_cli(commands)
            assert result.returncode == 0

            session_path = os.path.join(SAVED_SESSIONS_DIR, f"{session_name}.sh")
            with open(session_path) as f:
                content = f.read()

            # Should have shebang
            assert content.startswith("#!/bin/bash\n")

            # Should have the commands (not meta commands)
            assert 'new-network --name TestNet --username alice --devicename laptop' in content
            assert 'send "hello world"' in content
            assert 'invite' in content

            # Should NOT have save-session itself
            assert 'save-session' not in content
        finally:
            cleanup_session_file(session_name)

    def test_save_session_excludes_meta_commands(self):
        """Meta commands (help, status, etc.) are not saved."""
        session_name = "test_excludes_meta"
        cleanup_session_file(session_name)

        try:
            commands = f"""
new-network --name TestNet --username alice --devicename laptop
help
status
accounts
messages
send hello
save-session {session_name}
"""
            result = run_cli(commands)
            assert result.returncode == 0

            session_path = os.path.join(SAVED_SESSIONS_DIR, f"{session_name}.sh")
            with open(session_path) as f:
                content = f.read()

            # Should only have 2 actual commands
            assert "saved 2 commands" in result.stdout

            # Should NOT have meta commands
            assert '\nhelp\n' not in content
            assert '\nstatus\n' not in content
            assert '\naccounts\n' not in content
            assert '\nmessages\n' not in content
        finally:
            cleanup_session_file(session_name)

    def test_save_session_replay(self):
        """Saved session can be replayed and produces same result."""
        session_name = "test_replay"
        cleanup_session_file(session_name)

        try:
            # First run: create and save session
            commands = f"""
new-network --name ReplayTest --username alice --devicename laptop
send "test message for replay"
save-session {session_name}
"""
            result1 = run_cli(commands)
            assert result1.returncode == 0

            # Second run: replay saved session
            session_path = os.path.join(SAVED_SESSIONS_DIR, f"{session_name}.sh")
            with open(session_path) as f:
                saved_commands = f.read()

            result2 = run_cli(saved_commands)
            assert result2.returncode == 0

            # Should see the same message in replay
            assert "test message for replay" in result2.stdout
            assert "alice (laptop)" in result2.stdout
        finally:
            cleanup_session_file(session_name)

    def test_save_session_no_commands_error(self):
        """save-session with no commands to save shows error."""
        commands = """
save-session empty_test
"""
        result = run_cli(commands)

        # Should show error
        assert "error: no commands to save" in result.stdout

    def test_save_session_missing_name_error(self):
        """save-session without name shows usage."""
        commands = """
new-network --name TestNet --username alice --devicename laptop
save-session
"""
        result = run_cli(commands)

        assert "usage: save-session <name>" in result.stdout

    def test_save_session_sanitizes_filename(self):
        """Special characters in name are sanitized."""
        session_name = "test/with:special*chars"
        expected_name = "test_with_special_chars"
        cleanup_session_file(expected_name)

        try:
            commands = f"""
new-network --name TestNet --username alice --devicename laptop
save-session {session_name}
"""
            result = run_cli(commands)
            assert result.returncode == 0

            # Should create file with sanitized name
            session_path = os.path.join(SAVED_SESSIONS_DIR, f"{expected_name}.sh")
            assert os.path.exists(session_path)
        finally:
            cleanup_session_file(expected_name)

    def test_save_session_in_help(self):
        """save-session command appears in help text."""
        commands = "help"
        result = run_cli(commands)

        assert "save-session" in result.stdout
        assert "saved-sessions" in result.stdout
