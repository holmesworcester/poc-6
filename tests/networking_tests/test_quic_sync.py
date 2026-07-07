"""End-to-end QUIC relay sync tests using multiprocess clients."""
import socket
import subprocess
import time

import pytest

from core.quic import QuicRelayServer
from tests.networking_tests.multiprocess_client import RemoteClient, tick_all, assert_eventually


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


@pytest.fixture
def relay(tmp_path):
    try:
        import aioquic  # noqa: F401
    except Exception:
        pytest.skip("aioquic not installed")

    cert_path, key_path = generate_cert(tmp_path)
    port = get_free_port()
    relay = QuicRelayServer("127.0.0.1", port, cert_path, key_path)
    relay.start()
    yield f"quic://127.0.0.1:{port}"
    relay.stop()


@pytest.fixture
def alice(tmp_path):
    client = RemoteClient(
        name="alice",
        db_path=str(tmp_path / "alice.db"),
        udp_port=get_free_port()
    )
    client.start()
    yield client
    client.stop()


@pytest.fixture
def bob(tmp_path):
    client = RemoteClient(
        name="bob",
        db_path=str(tmp_path / "bob.db"),
        udp_port=get_free_port()
    )
    client.start()
    yield client
    client.stop()


def test_quic_relay_message_sync(relay, alice, bob):
    t_ms = 1000

    alice.new_network(name="Alice", t_ms=t_ms)
    t_ms += 100

    invite_link = alice.create_invite(t_ms=t_ms)
    t_ms += 100

    bob.create_peer(t_ms=t_ms)
    t_ms += 100
    bob.join(invite_link=invite_link, name="Bob", t_ms=t_ms)
    t_ms += 100

    # Switch to QUIC relay transport for both clients
    alice.start_quic(relay_url=relay, insecure=True)
    bob.start_quic(relay_url=relay, insecure=True)

    # Establish connection
    tick_all(alice, bob, t_ms=t_ms, rounds=50)
    t_ms += 5000

    alice.send_message(content="Hello over QUIC!", t_ms=t_ms)
    t_ms += 100

    def bob_has_message():
        msgs = bob.get_messages(channel_id=alice.channel_id)
        return any(m.get("content") == "Hello over QUIC!" for m in msgs)

    assert_eventually(
        bob_has_message,
        alice, bob,
        t_ms=t_ms,
        max_rounds=200,
        msg="Bob should receive QUIC-relayed message"
    )
