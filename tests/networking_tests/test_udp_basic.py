"""
Basic UDP tests to verify the fundamentals work.
"""
import pytest
import time
import socket
from multiprocessing import Process, Queue
from tests.networking_tests.ipc import make_queue_pair, supports_udp_in_subprocess

pytestmark = pytest.mark.skipif(
    not supports_udp_in_subprocess(),
    reason="UDP sockets are not permitted in subprocesses on this platform",
)


def udp_sender(port: int, message: bytes, result_queue):
    """Send a UDP packet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(message, ('127.0.0.1', port))
    sock.close()
    result_queue.put({'sent': True})


def udp_receiver(port: int, result_queue):
    """Receive a UDP packet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port))
    sock.settimeout(2.0)
    try:
        data, addr = sock.recvfrom(65535)
        result_queue.put({'received': data, 'from': addr})
    except socket.timeout:
        result_queue.put({'timeout': True})
    finally:
        sock.close()


class TestBasicUDP:
    """Test raw UDP works between processes."""

    def test_udp_send_receive(self):
        """Basic UDP packet delivery between processes."""
        port = 19100
        message = b"hello from sender"

        recv_sender, recv_queue = make_queue_pair()
        send_sender, send_queue = make_queue_pair()

        # Start receiver first
        receiver = Process(target=udp_receiver, args=(port, recv_sender))
        receiver.start()
        time.sleep(0.1)  # Let it bind

        # Send
        sender = Process(target=udp_sender, args=(port, message, send_sender))
        sender.start()

        # Wait for results
        sender.join(timeout=2)
        receiver.join(timeout=2)

        send_result = send_queue.get(timeout=1)
        recv_result = recv_queue.get(timeout=1)

        assert send_result.get('sent') == True
        assert recv_result.get('received') == message
        print(f"UDP delivery works: {message} -> {recv_result}")


class TestTransportUDP:
    """Test transport layer UDP functions."""

    def test_transport_udp_roundtrip(self):
        """Transport UDP send/receive between two processes."""
        from multiprocessing import Process

        def worker(name: str, port: int, cmd_queue: Queue, result_queue: Queue):
            """Worker with transport UDP."""
            from core import transport

            transport.start_udp('127.0.0.1', port)
            result_queue.put({'started': True, 'port': port})

            while True:
                cmd = cmd_queue.get(timeout=5)
                if cmd['action'] == 'send':
                    transport.send(cmd['data'], ('127.0.0.1', port), cmd['to_addr'])
                    transport.udp_transfer()
                    result_queue.put({'sent': True})
                elif cmd['action'] == 'receive':
                    transport.udp_transfer()
                    batch = transport.drain_incoming(10)
                    result_queue.put({'received': batch})
                elif cmd['action'] == 'stop':
                    transport.stop_udp()
                    result_queue.put({'stopped': True})
                    break

        # Create workers
        alice_cmd_send, alice_cmd_recv = make_queue_pair()
        alice_res_send, alice_res_recv = make_queue_pair()
        bob_cmd_send, bob_cmd_recv = make_queue_pair()
        bob_res_send, bob_res_recv = make_queue_pair()

        alice = Process(target=worker, args=('alice', 19101, alice_cmd_recv, alice_res_send))
        bob = Process(target=worker, args=('bob', 19102, bob_cmd_recv, bob_res_send))

        alice.start()
        bob.start()

        # Wait for both to start
        alice_res_recv.get(timeout=2)
        bob_res_recv.get(timeout=2)
        time.sleep(0.1)

        try:
            # Alice sends to Bob
            alice_cmd_send.put({'action': 'send', 'data': b'hello bob', 'to_addr': ('127.0.0.1', 19102)})
            alice_res_recv.get(timeout=2)

            time.sleep(0.1)

            # Bob receives
            bob_cmd_send.put({'action': 'receive'})
            recv = bob_res_recv.get(timeout=2)

            assert len(recv['received']) == 1, f"Bob should have 1 packet, got {recv}"
            assert recv['received'][0][0] == b'hello bob'
            print(f"Transport UDP works: {recv}")

        finally:
            alice_cmd_send.put({'action': 'stop'})
            bob_cmd_send.put({'action': 'stop'})
            alice.join(timeout=2)
            bob.join(timeout=2)


class TestConnectionBasic:
    """Test connection establishment basics."""

    def test_connection_works(self):
        """Verify connection establishment over real UDP."""
        from tests.networking_tests.multiprocess_client import RemoteClient, tick_all
        import tempfile
        import os

        tmp = tempfile.mkdtemp()

        alice = RemoteClient(
            name="alice",
            db_path=os.path.join(tmp, "alice.db"),
            udp_port=19103
        )
        bob = RemoteClient(
            name="bob",
            db_path=os.path.join(tmp, "bob.db"),
            udp_port=19104
        )

        alice.start()
        bob.start()

        try:
            t_ms = 1000

            # Alice creates network
            alice.new_network(name='Alice', t_ms=t_ms)
            t_ms += 50

            # Alice creates invite
            invite_link = alice.create_invite(t_ms=t_ms)
            t_ms += 50

            # Bob learns Alice's address out-of-band
            bob.add_peer_address(alice.peer_shared_id, '127.0.0.1', alice.udp_port)

            # Bob creates peer and joins
            bob.create_peer(t_ms=t_ms)
            t_ms += 50

            bob.join(invite_link=invite_link, name='Bob', t_ms=t_ms)
            t_ms += 50

            # Alice learns Bob's address
            alice.add_peer_address(bob.peer_shared_id, '127.0.0.1', bob.udp_port)

            # Run ticks
            tick_all(alice, bob, t_ms=t_ms, rounds=20)
            t_ms += 2000

            # Check connections
            alice_conns = alice.get_connections(t_ms=t_ms)
            bob_conns = bob.get_connections(t_ms=t_ms)

            print(f"Alice connections: {alice_conns}")
            print(f"Bob connections: {bob_conns}")

            # Both should have active connections
            alice_active = [c for c in alice_conns if c['can_send']]
            bob_active = [c for c in bob_conns if c['can_send']]

            assert len(alice_active) >= 1, f"Alice should have active connection"
            assert len(bob_active) >= 1, f"Bob should have active connection"

        finally:
            alice.stop()
            bob.stop()
