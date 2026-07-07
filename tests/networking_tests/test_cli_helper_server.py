"""
CLI helper server onboarding over UDP.

This test exercises:
- helper-add CLI command posting an invite to a server endpoint
- helper server joining via its invite endpoint
- UDP sync between separate CLI instances
- helper server receives no group keys or decrypted messages
"""
import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from http import client


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CLI_PATH = os.path.join(REPO_ROOT, 'cli.py')


def get_free_port():
    """Get a free port for UDP or HTTP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for_invite_endpoint(port: int, timeout: float = 10.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/submit-invite")
            conn.getresponse()
            conn.close()
            return
        except Exception:
            time.sleep(0.1)
    raise AssertionError("Timed out waiting for helper invite endpoint")


def parse_invite_id(invite_link: str) -> str:
    if not invite_link.startswith("quiet://invite/"):
        raise AssertionError(f"Unexpected invite link: {invite_link}")
    code = invite_link.replace("quiet://invite/", "")
    padding = "=" * ((4 - len(code) % 4) % 4)
    data = json.loads(base64.urlsafe_b64decode(code + padding).decode())
    return data["invite_id"]


def parse_invite_link(output: str) -> str:
    for line in output.splitlines():
        if "link:" in line:
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"invite link not found in output:\n{output}")


def run_cli(db_path: str, command: str, listen_port: int = None, peer_addrs: list = None, timeout: float = 15.0) -> str:
    cmd = [sys.executable, CLI_PATH, "--db-path", db_path, "-e", command]

    if listen_port:
        cmd.extend(["--listen", f"127.0.0.1:{listen_port}"])

    if peer_addrs:
        for addr in peer_addrs:
            cmd.extend(["--peer", addr])

    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=REPO_ROOT
    )
    return result.stdout + result.stderr


def run_cli_daemon(db_path: str, listen_port: int, peer_addrs: list = None) -> subprocess.Popen:
    cmd = [sys.executable, CLI_PATH, "--db-path", db_path, "--listen", f"127.0.0.1:{listen_port}", "--sync-only"]

    if peer_addrs:
        for addr in peer_addrs:
            cmd.extend(["--peer", addr])

    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT
    )


def run_server_daemon(db_path: str, invite_port: int, listen_port: int, server_name: str = "helper",
                      server_device: str = "server") -> subprocess.Popen:
    cmd = [
        sys.executable,
        CLI_PATH,
        "--db-path",
        db_path,
        "--server",
        "--invite-listen",
        f"127.0.0.1:{invite_port}",
        "--listen",
        f"127.0.0.1:{listen_port}",
        "--server-name",
        server_name,
        "--server-device",
        server_device,
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT
    )


def parse_peer_shared_id(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("peer_shared_id:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"peer_shared_id not found in output:\n{output}")


def get_peer_id(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT peer_id FROM local_peers LIMIT 1").fetchone()
        if not row:
            raise AssertionError("No peer_id found in local_peers")
        return row[0]
    finally:
        conn.close()


def invite_accepted_exists(db_path: str, invite_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM invite_accepteds WHERE invite_id = ? LIMIT 1",
            (invite_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def count_rows(db_path: str, table: str, peer_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE recorded_by = ?",
            (peer_id,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def connection_ready(db_path: str, peer_id: str, remote_peer_shared_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """SELECT 1 FROM connections
               WHERE recorded_by = ? AND peer_shared_id = ? AND their_key IS NOT NULL
               LIMIT 1""",
            (peer_id, remote_peer_shared_id)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def wait_for_invite_accepted(db_path: str, invite_id: str, timeout: float = 20.0):
    start = time.time()
    while time.time() - start < timeout:
        if invite_accepted_exists(db_path, invite_id):
            return
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for helper server to accept invite")


def wait_for_connection(db_path: str, peer_id: str, remote_peer_shared_id: str, timeout: float = 20.0):
    start = time.time()
    while time.time() - start < timeout:
        if connection_ready(db_path, peer_id, remote_peer_shared_id):
            return
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for helper server connection handshake")


def test_cli_helper_add_server_privacy(tmp_path):
    alice_db = str(tmp_path / "alice.db")
    helper_db = str(tmp_path / "helper.db")

    alice_port = get_free_port()
    helper_port = get_free_port()
    invite_port = get_free_port()

    alice_daemon = None
    helper_server = None

    try:
        helper_server = run_server_daemon(helper_db, invite_port, helper_port)
        wait_for_invite_endpoint(invite_port)

        # Alice creates network
        run_cli(alice_db, "new-network --name TestNet --username alice --devicename laptop")
        whoami = run_cli(alice_db, "whoami")
        alice_peer_shared_id = parse_peer_shared_id(whoami)

        # Start Alice sync daemon
        alice_daemon = run_cli_daemon(alice_db, alice_port)

        # Create helper invite and submit to server endpoint
        endpoint = f"http://127.0.0.1:{invite_port}/submit-invite"
        output = run_cli(
            alice_db,
            f"helper-add --endpoint {endpoint} --address 127.0.0.1 --port {alice_port}"
        )
        assert "submitted helper invite" in output.lower(), f"helper-add failed: {output}"

        invite_link = parse_invite_link(output)
        invite_id = parse_invite_id(invite_link)

        # Wait for helper server to accept invite and connect
        wait_for_invite_accepted(helper_db, invite_id)
        helper_peer_id = get_peer_id(helper_db)
        wait_for_connection(helper_db, helper_peer_id, alice_peer_shared_id)

        # Send a message from Alice
        run_cli(alice_db, "send Hello helper!")

        helper_keys = count_rows(helper_db, "group_keys", helper_peer_id)
        helper_messages = count_rows(helper_db, "messages", helper_peer_id)

        assert helper_keys == 0, "Helper should not receive group keys"
        assert helper_messages == 0, "Helper should not decrypt messages"

    finally:
        if helper_server:
            helper_server.terminate()
            helper_server.wait(timeout=5)
        if alice_daemon:
            alice_daemon.terminate()
            alice_daemon.wait(timeout=5)
