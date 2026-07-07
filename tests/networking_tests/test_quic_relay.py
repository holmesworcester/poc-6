"""QUIC relay transport smoke test."""
import os
import socket
import subprocess
import time

import pytest

from core.quic import QuicRelayClient, QuicRelayServer, QuicUnavailableError


def get_free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def generate_cert(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-subj", "/CN=localhost",
            "-days", "1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(cert_path), str(key_path)


def wait_for_message(client: QuicRelayClient, timeout: float = 5.0):
    start = time.time()
    while time.time() - start < timeout:
        items = client.drain()
        if items:
            return items[0]
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for QUIC relay message")


def test_quic_relay_smoke(tmp_path):
    try:
        import aioquic  # noqa: F401
    except Exception:
        pytest.skip("aioquic not installed")

    cert_path, key_path = generate_cert(tmp_path)
    port = get_free_port()
    relay = QuicRelayServer("127.0.0.1", port, cert_path, key_path)
    relay.start()

    alice = QuicRelayClient(f"quic://127.0.0.1:{port}", "alice", insecure=True)
    bob = QuicRelayClient(f"quic://127.0.0.1:{port}", "bob", insecure=True)
    try:
        alice.start()
        bob.start()
        alice.send("bob", b"hello-quic")
        payload = wait_for_message(bob)
        assert payload == b"hello-quic"
    finally:
        alice.stop()
        bob.stop()
        relay.stop()
