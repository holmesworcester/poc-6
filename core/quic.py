"""QUIC relay transport (client + server).

Uses aioquic for QUIC datagrams. The relay is a thin router that forwards
opaque transit blobs based on a small header containing `to_peer_id`.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import queue
import ssl
import struct
import threading
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ALPN_PROTOCOL = "quiet-relay"
FRAME_VERSION = 1
FRAME_TYPE_HELLO = 1
FRAME_TYPE_DATA = 2

try:  # pragma: no cover - import guard
    from aioquic.asyncio import connect, serve
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import DatagramFrameReceived, ConnectionTerminated, HandshakeCompleted
    AIOQUIC_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    QuicConnectionProtocol = object  # type: ignore[assignment]
    AIOQUIC_AVAILABLE = False


class QuicUnavailableError(RuntimeError):
    """Raised when aioquic is not installed."""


def _require_aioquic() -> None:
    if not AIOQUIC_AVAILABLE:  # pragma: no cover - import guard
        raise QuicUnavailableError(
            "aioquic is required for QUIC transport. Install with: pip install aioquic"
        )


def _parse_relay_url(url: str) -> tuple[str, int]:
    if "://" not in url:
        url = f"quic://{url}"
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        raise ValueError(f"Invalid relay URL: {url}")
    return host, port


def _encode_frame(frame_type: int, header: dict, payload: bytes = b"") -> bytes:
    header_json = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(header_json) > 65535:
        raise ValueError("QUIC header too large")
    return struct.pack("!BBH", FRAME_VERSION, frame_type, len(header_json)) + header_json + payload


def _decode_frame(data: bytes) -> tuple[int, dict, bytes]:
    if len(data) < 4:
        raise ValueError("QUIC frame too short")
    version, frame_type, header_len = struct.unpack("!BBH", data[:4])
    if version != FRAME_VERSION:
        raise ValueError(f"Unsupported QUIC frame version: {version}")
    if len(data) < 4 + header_len:
        raise ValueError("QUIC frame header truncated")
    header = json.loads(data[4:4 + header_len].decode("utf-8"))
    payload = data[4 + header_len:]
    return frame_type, header, payload


@dataclass
class RelayState:
    """Shared relay state mapping peer_id to protocol."""
    peers: dict[str, "RelayServerProtocol"]


if AIOQUIC_AVAILABLE:  # pragma: no cover - requires aioquic

    class RelayServerProtocol(QuicConnectionProtocol):
        """Server-side QUIC protocol for relaying datagrams."""
        def __init__(self, relay_state: RelayState, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._relay_state = relay_state
            self.peer_id: Optional[str] = None

        def quic_event_received(self, event):
            if isinstance(event, DatagramFrameReceived):
                try:
                    frame_type, header, payload = _decode_frame(event.data)
                except Exception as exc:
                    log.warning(f"quic relay: bad datagram: {exc}")
                    return

                if frame_type == FRAME_TYPE_HELLO:
                    peer_id = header.get("peer_id")
                    if peer_id:
                        self.peer_id = peer_id
                        self._relay_state.peers[peer_id] = self
                        log.info(f"quic relay: registered {peer_id[:20]}...")
                    return

                if frame_type == FRAME_TYPE_DATA:
                    to_peer_id = header.get("to_peer_id")
                    if not to_peer_id:
                        return
                    target = self._relay_state.peers.get(to_peer_id)
                    if not target:
                        return
                    out_header = {"from_peer_id": self.peer_id} if self.peer_id else {}
                    out_frame = _encode_frame(FRAME_TYPE_DATA, out_header, payload)
                    target.send_datagram(out_frame)
                    return

            if isinstance(event, ConnectionTerminated):
                if self.peer_id and self._relay_state.peers.get(self.peer_id) is self:
                    self._relay_state.peers.pop(self.peer_id, None)

        def send_datagram(self, data: bytes) -> None:
            self._quic.send_datagram_frame(data)
            self.transmit()


    class RelayClientProtocol(QuicConnectionProtocol):
        """Client-side QUIC protocol for relay transport."""
        def __init__(self, peer_id: str, incoming: queue.Queue, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._peer_id = peer_id
            self._incoming = incoming

        def quic_event_received(self, event):
            if isinstance(event, HandshakeCompleted):
                hello = _encode_frame(FRAME_TYPE_HELLO, {"peer_id": self._peer_id})
                self.send_datagram(hello)

            if isinstance(event, DatagramFrameReceived):
                try:
                    frame_type, header, payload = _decode_frame(event.data)
                except Exception as exc:
                    log.warning(f"quic client: bad datagram: {exc}")
                    return
                if frame_type == FRAME_TYPE_DATA:
                    self._incoming.put(payload)

        def send_datagram(self, data: bytes) -> None:
            self._quic.send_datagram_frame(data)
            self.transmit()

else:  # pragma: no cover - import guard

    class RelayServerProtocol:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            _require_aioquic()

    class RelayClientProtocol:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            _require_aioquic()


class QuicRelayClient:
    """Background-thread QUIC relay client."""
    def __init__(self, relay_url: str, peer_id: str, insecure: bool = False):
        _require_aioquic()
        self._relay_url = relay_url
        self._peer_id = peer_id
        self._insecure = insecure
        self._incoming: queue.Queue[bytes] = queue.Queue()
        self._outgoing: Optional["asyncio.Queue[tuple[str, bytes] | None]"] = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: Optional[Exception] = None
        self._protocol: Optional[RelayClientProtocol] = None
        self._host, self._port = _parse_relay_url(relay_url)

    def start(self) -> None:
        import asyncio

        if self._thread:
            return

        def runner():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._outgoing = asyncio.Queue()
                loop.run_until_complete(self._run())
            except Exception as exc:
                self._error = exc
                log.error(f"quic client: failed to start: {exc}")
                self._ready.set()
            finally:
                self._ready.set()

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise TimeoutError("Timed out starting QUIC relay client")
        if self._error:
            raise self._error

    async def _run(self):  # pragma: no cover - requires aioquic
        import asyncio

        config = QuicConfiguration(is_client=True, alpn_protocols=[ALPN_PROTOCOL])
        config.max_datagram_frame_size = 65536
        if self._insecure:
            config.verify_mode = ssl.CERT_NONE

        async with connect(
            self._host,
            self._port,
            configuration=config,
            create_protocol=lambda *args, **kwargs: RelayClientProtocol(self._peer_id, self._incoming, *args, **kwargs),
        ) as protocol:
            self._protocol = protocol
            wait_connected = getattr(protocol, "wait_connected", None)
            if wait_connected:
                await wait_connected()
            self._ready.set()
            while not self._stop.is_set():
                try:
                    item = await asyncio.wait_for(self._outgoing.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    break
                to_peer_id, blob = item
                header = {"to_peer_id": to_peer_id}
                frame = _encode_frame(FRAME_TYPE_DATA, header, blob)
                protocol.send_datagram(frame)
            protocol.close()

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._outgoing:
            self._loop.call_soon_threadsafe(self._outgoing.put_nowait, None)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def send(self, to_peer_id: str, blob: bytes) -> None:
        if not self._loop or not self._outgoing:
            raise RuntimeError("QUIC relay client not started")
        self._loop.call_soon_threadsafe(self._outgoing.put_nowait, (to_peer_id, blob))

    def drain(self, limit: int = 100) -> list[bytes]:
        items = []
        while len(items) < limit:
            try:
                items.append(self._incoming.get_nowait())
            except queue.Empty:
                break
        return items

    @property
    def relay_addr(self) -> tuple[str, int]:
        return (self._host, self._port)


class QuicRelayServer:
    """Background-thread QUIC relay server."""
    def __init__(self, host: str, port: int, cert_path: str, key_path: str):
        _require_aioquic()
        self._host = host
        self._port = port
        self._cert_path = cert_path
        self._key_path = key_path
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._error: Optional[Exception] = None
        self._server = None
        self._relay_state = RelayState(peers={})

    def start(self) -> None:
        import asyncio
        if self._thread:
            return

        def runner():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                config = QuicConfiguration(is_client=False, alpn_protocols=[ALPN_PROTOCOL])
                config.max_datagram_frame_size = 65536
                config.load_cert_chain(self._cert_path, self._key_path)
                self._server = loop.run_until_complete(
                    serve(
                        self._host,
                        self._port,
                        configuration=config,
                        create_protocol=lambda *args, **kwargs: RelayServerProtocol(self._relay_state, *args, **kwargs),
                    )
                )
                self._ready.set()
                loop.run_forever()
            except Exception as exc:
                self._error = exc
                log.error(f"quic relay: failed to start: {exc}")
                self._ready.set()

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise TimeoutError("Timed out starting QUIC relay server")
        if self._error:
            raise self._error

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
