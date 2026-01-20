"""UDP socket for real network communication."""
import socket
import threading
import queue
from typing import Optional
import logging

log = logging.getLogger(__name__)


class UDPSocket:
    """Simple UDP socket wrapper for network I/O.

    Thread-safe: background thread receives packets into queue,
    main thread sends and drains.
    """

    def __init__(self, host: str, port: int):
        """Initialize UDP socket.

        Args:
            host: Host to bind to (e.g., '0.0.0.0' for all interfaces)
            port: Port to listen on
        """
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._incoming: queue.Queue = queue.Queue()
        self._running = False
        self._recv_thread: Optional[threading.Thread] = None

    def start(self):
        """Start listening for UDP packets."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.1)  # Non-blocking with timeout for clean shutdown
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        log.info(f"UDPSocket: listening on {self.host}:{self.port}")

    def stop(self):
        """Stop the socket and receiver thread."""
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()
        log.info(f"UDPSocket: stopped")

    def _recv_loop(self):
        """Background thread: receive packets and queue them."""
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
                self._incoming.put((data, addr))
            except socket.timeout:
                continue
            except OSError:
                break  # Socket closed

    def send_to(self, addr: tuple, data: bytes):
        """Send packet to an address.

        Args:
            addr: Destination (host, port) tuple
            data: Packet data
        """
        if self._sock:
            self._sock.sendto(data, addr)

    def drain(self) -> list:
        """Drain all queued packets.

        Returns:
            List of (data, addr) tuples
        """
        packets = []
        while True:
            try:
                packets.append(self._incoming.get_nowait())
            except queue.Empty:
                break
        return packets

    def has_packets(self) -> bool:
        """Check if there are packets waiting."""
        return not self._incoming.empty()
