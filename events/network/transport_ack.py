"""Transport-level ACK for flow control.

This is a control event used by transport flow control. It is not
projected or stored; it's handled immediately at ingest time.
"""
from typing import Any

from core import transport
from core import wire_format


EVENT_TYPE = "transport_ack"
SHAREABLE = False
PROJECTION_TABLE = None

EVENT_SPEC = {
    "encrypted": False,
    "signer": None,
    "requires": {},
    "optional": {},
}


def encode_wire_event(*, ack_count: int, created_at_ms: int) -> bytes:
    return wire_format.encode_transport_ack_wire_event(
        ack_count=ack_count,
        created_at_ms=created_at_ms,
    )


def decode_wire_event(event_blob: bytes) -> dict[str, Any]:
    return wire_format.decode_transport_ack_wire_event(event_blob)


def handle_incoming(event_blob: bytes, flow_key: str) -> None:
    data = decode_wire_event(event_blob)
    transport.flow_on_ack(flow_key, data.get("ack_count", 0))
