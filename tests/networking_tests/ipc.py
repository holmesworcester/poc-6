"""IPC helpers for networking tests.

Provides a Queue-like interface without requiring POSIX semaphores.
Falls back to Pipe-based queues when multiprocessing.Queue is unavailable.
"""
from __future__ import annotations

from multiprocessing import Pipe, Queue, Process
import queue as std_queue
from typing import Any, Tuple


class PipeQueue:
    """Minimal Queue-like wrapper around multiprocessing.Connection.

    Supports put() and get(timeout=...).
    """

    def __init__(self, recv_conn=None, send_conn=None):
        self._recv = recv_conn
        self._send = send_conn

    def put(self, item: Any) -> None:
        if self._send is None:
            raise RuntimeError("PipeQueue has no send connection")
        self._send.send(item)

    def get(self, timeout: float | None = None) -> Any:
        if self._recv is None:
            raise RuntimeError("PipeQueue has no recv connection")
        if timeout is None:
            return self._recv.recv()
        if self._recv.poll(timeout):
            return self._recv.recv()
        raise std_queue.Empty


def make_queue_pair() -> Tuple[Any, Any]:
    """Return (sender, receiver) queue endpoints.

    - sender supports .put()
    - receiver supports .get()
    """
    try:
        q = Queue()
        return q, q
    except (PermissionError, OSError):
        recv_conn, send_conn = Pipe(duplex=False)
        sender = PipeQueue(send_conn=send_conn)
        receiver = PipeQueue(recv_conn=recv_conn)
        return sender, receiver


_UDP_SUBPROCESS_OK: bool | None = None
_UDP_SOCKET_OK: bool | None = None


def _probe_udp_socket(conn) -> None:
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.close()
        conn.send(True)
    except Exception:
        conn.send(False)


def supports_udp_in_subprocess() -> bool:
    """Return True if a subprocess can open a UDP socket."""
    global _UDP_SUBPROCESS_OK
    if _UDP_SUBPROCESS_OK is not None:
        return _UDP_SUBPROCESS_OK
    try:
        recv_conn, send_conn = Pipe(duplex=False)
        proc = Process(target=_probe_udp_socket, args=(send_conn,))
        proc.start()
        result = recv_conn.recv() if recv_conn.poll(1.0) else False
        proc.join(timeout=1.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=1.0)
        _UDP_SUBPROCESS_OK = bool(result)
    except Exception:
        _UDP_SUBPROCESS_OK = False
    return _UDP_SUBPROCESS_OK


def supports_udp_socket() -> bool:
    """Return True if a UDP socket can be opened in this process."""
    global _UDP_SOCKET_OK
    if _UDP_SOCKET_OK is not None:
        return _UDP_SOCKET_OK
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.close()
        _UDP_SOCKET_OK = True
    except Exception:
        _UDP_SOCKET_OK = False
    return _UDP_SOCKET_OK
