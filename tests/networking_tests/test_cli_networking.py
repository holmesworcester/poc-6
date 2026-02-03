"""
CLI networking tests - multiple CLI instances communicating over UDP.

Each CLI runs in a separate process with its own database and UDP socket.
Tests verify the full CLI flow including invite links and message sync.
"""
import pytest
import subprocess
import tempfile
import os
import time
import socket
import re
import sys
from pathlib import Path
from tests.networking_tests.ipc import supports_udp_in_subprocess

pytestmark = pytest.mark.skipif(
    not supports_udp_in_subprocess(),
    reason="UDP sockets are not permitted in subprocesses on this platform",
)


def get_free_port():
    """Get a free port for UDP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for_condition(condition_fn, timeout: float = 30.0, interval: float = 1.0, description: str = "condition"):
    """Wait for a condition function to return True.

    Args:
        condition_fn: Function that returns (success: bool, result_or_error: str)
        timeout: Max time to wait in seconds
        interval: Time between retries in seconds
        description: Description for error message

    Returns:
        The result from condition_fn on success

    Raises:
        AssertionError: If timeout expires
    """
    start = time.time()
    last_error = None
    while time.time() - start < timeout:
        try:
            success, result = condition_fn()
            if success:
                return result
            last_error = result
        except Exception as e:
            last_error = str(e)
        time.sleep(interval)
    raise AssertionError(f"Timeout waiting for {description}: {last_error}")


def run_cli(db_path: str, command: str, listen_port: int = None, peer_addrs: list = None, timeout: float = 10.0) -> str:
    """Run a CLI command and return output."""
    cmd = [sys.executable, str(CLI_PATH), '--db', db_path, '-e', command]
    
    if listen_port:
        cmd.extend(['--listen', f'127.0.0.1:{listen_port}'])
    
    if peer_addrs:
        for addr in peer_addrs:
            cmd.extend(['--peer', addr])
    
    env = os.environ.copy()
    env['PYTHONPATH'] = '.'
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO_ROOT)
    )
    return result.stdout + result.stderr


def run_cli_daemon(db_path: str, listen_port: int, peer_addrs: list = None, duration: float = 5.0) -> subprocess.Popen:
    """Start CLI in sync daemon mode. Returns Popen object."""
    cmd = [sys.executable, str(CLI_PATH), '--db', db_path, '--listen', f'127.0.0.1:{listen_port}', '--sync-only']
    
    if peer_addrs:
        for addr in peer_addrs:
            cmd.extend(['--peer', addr])
    
    env = os.environ.copy()
    env['PYTHONPATH'] = '.'
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO_ROOT)
    )
    return proc


class TestCLITwoPlayer:
    """Tests with two CLI instances."""

    def test_alice_bob_message_sync(self, tmp_path):
        """Alice creates network, Bob joins, Alice sends message, Bob receives."""
        alice_db = str(tmp_path / "alice.db")
        bob_db = str(tmp_path / "bob.db")
        alice_port = get_free_port()
        bob_port = get_free_port()

        # Alice creates network
        output = run_cli(alice_db, "new-network --name TestNet --username Alice --devicename Laptop")
        assert "created network" in output.lower() or "network_id" in output.lower(), f"Failed to create network: {output}"

        # Get Alice's peer_shared_id
        whoami = run_cli(alice_db, "whoami")
        alice_peer_shared_id = None
        for line in whoami.split('\n'):
            if 'peer_shared_id:' in line:
                alice_peer_shared_id = line.split(':')[1].strip()
                break
        assert alice_peer_shared_id, f"Could not find Alice's peer_shared_id in: {whoami}"

        # Alice creates invite
        invite_output = run_cli(alice_db, "invite")
        # Extract invite link from output (format: "  link: quiet://...")
        invite_link = None
        for line in invite_output.split('\n'):
            if 'link:' in line and 'quiet://' in line:
                invite_link = line.split('link:')[1].strip()
                break
        assert invite_link, f"Could not find invite link in: {invite_output}"

        # Bob joins using invite (no --peer yet, just creates local state)
        bob_join = run_cli(bob_db, f"accept-invite --username Bob --devicename Phone --invite {invite_link}")
        assert "joined" in bob_join.lower(), f"Bob failed to join: {bob_join}"

        # Start both daemons
        # - Alice listens, doesn't know Bob's address yet (learns from packet metadata)
        # - Bob connects to Alice using her peer_shared_id from the invite
        alice_daemon = run_cli_daemon(alice_db, alice_port)
        time.sleep(0.5)
        bob_daemon = run_cli_daemon(bob_db, bob_port, peer_addrs=[f"{alice_peer_shared_id}@127.0.0.1:{alice_port}"])
        
        try:
            # Alice sends a message
            send_output = run_cli(alice_db, "send Hello Bob from CLI!")
            print(f"Send output: {send_output}")

            # Wait for Bob to receive the message
            def check_bob_messages():
                msgs = run_cli(bob_db, "messages")
                if "Hello Bob from CLI!" in msgs:
                    return True, msgs
                return False, msgs

            bob_messages = wait_for_condition(
                check_bob_messages,
                timeout=30.0,
                interval=1.0,
                description="Bob to receive Alice's message"
            )
            print(f"Bob messages: {bob_messages}")

        finally:
            # Terminate and capture daemon outputs for debugging
            alice_daemon.terminate()
            bob_daemon.terminate()
            try:
                alice_out, alice_err = alice_daemon.communicate(timeout=2)
                print(f"Alice daemon stdout: {alice_out[:1000] if alice_out else '(empty)'}")
                print(f"Alice daemon stderr: {alice_err[:1000] if alice_err else '(empty)'}")
            except:
                print("Alice daemon: could not get output")
            try:
                bob_out, bob_err = bob_daemon.communicate(timeout=2)
                print(f"Bob daemon stdout: {bob_out[:1000] if bob_out else '(empty)'}")
                print(f"Bob daemon stderr: {bob_err[:1000] if bob_err else '(empty)'}")
            except:
                print("Bob daemon: could not get output")


class TestCLIThreePlayer:
    """Tests with three CLI instances."""

    def test_three_way_sync(self, tmp_path):
        """Alice, Bob, Charlie all sync messages."""
        alice_db = str(tmp_path / "alice.db")
        bob_db = str(tmp_path / "bob.db")
        charlie_db = str(tmp_path / "charlie.db")
        alice_port = get_free_port()
        bob_port = get_free_port()
        charlie_port = get_free_port()

        # Alice creates network
        run_cli(alice_db, "new-network --name TestNet --username Alice --devicename Laptop")

        # Get Alice's peer_shared_id
        whoami = run_cli(alice_db, "whoami")
        alice_peer_shared_id = None
        for line in whoami.split('\n'):
            if 'peer_shared_id:' in line:
                alice_peer_shared_id = line.split(':')[1].strip()
                break
        assert alice_peer_shared_id

        # Alice creates invite for Bob
        invite_bob_output = run_cli(alice_db, "invite")
        invite_bob = None
        for line in invite_bob_output.split('\n'):
            if 'link:' in line and 'quiet://' in line:
                invite_bob = line.split('link:')[1].strip()
                break
        assert invite_bob, f"Could not find invite link in: {invite_bob_output}"

        # Bob joins
        bob_join = run_cli(bob_db, f"accept-invite --username Bob --devicename Phone --invite {invite_bob}")
        assert "joined" in bob_join.lower(), f"Bob failed to join: {bob_join}"

        # Alice creates invite for Charlie
        invite_charlie_output = run_cli(alice_db, "invite")
        invite_charlie = None
        for line in invite_charlie_output.split('\n'):
            if 'link:' in line and 'quiet://' in line:
                invite_charlie = line.split('link:')[1].strip()
                break
        assert invite_charlie, f"Could not find invite link in: {invite_charlie_output}"

        # Charlie joins
        charlie_join = run_cli(charlie_db, f"accept-invite --username Charlie --devicename Tablet --invite {invite_charlie}")
        assert "joined" in charlie_join.lower(), f"Charlie failed to join: {charlie_join}"

        # Start all daemons with more startup delay for connection establishment
        alice_daemon = run_cli_daemon(alice_db, alice_port)
        time.sleep(1.0)
        bob_daemon = run_cli_daemon(bob_db, bob_port, peer_addrs=[f"{alice_peer_shared_id}@127.0.0.1:{alice_port}"])
        time.sleep(1.0)
        charlie_daemon = run_cli_daemon(charlie_db, charlie_port, peer_addrs=[f"{alice_peer_shared_id}@127.0.0.1:{alice_port}"])
        time.sleep(1.0)  # Allow connections to establish

        try:
            # Step 1: Wait for Bob's channel to be ready (means sync completed)
            def check_bob_channel():
                out = run_cli(bob_db, "messages")
                # If we see "#general" channel info, Bob's sync is complete
                if "#general" in out or "no messages" in out.lower():
                    return True, out
                if "no channel selected" in out.lower():
                    return False, out
                return True, out  # Any other output means channel is ready

            def check_charlie_channel():
                out = run_cli(charlie_db, "messages")
                if "#general" in out or "no messages" in out.lower():
                    return True, out
                if "no channel selected" in out.lower():
                    return False, out
                return True, out

            wait_for_condition(check_bob_channel, timeout=60.0, interval=1.0,
                               description="Bob's channel to be ready")
            wait_for_condition(check_charlie_channel, timeout=60.0, interval=1.0,
                               description="Charlie's channel to be ready")

            # Step 2: All three send their messages
            run_cli(alice_db, "send Hello everyone!")
            run_cli(bob_db, "send Hi from Bob!")
            run_cli(charlie_db, "send Charlie here!")

            # Wait for all messages to sync to all peers
            expected_msgs = ["Hello everyone!", "Hi from Bob!", "Charlie here!"]

            def check_all_synced():
                alice_msgs = run_cli(alice_db, "messages")
                bob_msgs = run_cli(bob_db, "messages")
                charlie_msgs = run_cli(charlie_db, "messages")

                results = {"Alice": alice_msgs, "Bob": bob_msgs, "Charlie": charlie_msgs}
                missing = []
                for name, msgs in results.items():
                    for expected in expected_msgs:
                        if expected not in msgs:
                            missing.append(f"{name} missing '{expected}'")
                if missing:
                    return False, f"{', '.join(missing)}\nAlice: {alice_msgs}\nBob: {bob_msgs}\nCharlie: {charlie_msgs}"
                return True, results

            results = wait_for_condition(
                check_all_synced,
                timeout=90.0,
                interval=2.0,
                description="all messages to sync to all peers"
            )
            print(f"Alice messages:\n{results['Alice']}")
            print(f"Bob messages:\n{results['Bob']}")
            print(f"Charlie messages:\n{results['Charlie']}")

        finally:
            alice_daemon.terminate()
            bob_daemon.terminate()
            charlie_daemon.terminate()
            alice_daemon.wait(timeout=2)
            bob_daemon.wait(timeout=2)
            charlie_daemon.wait(timeout=2)
REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "cli.py"
