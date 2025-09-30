"""
Send to Network handler - Sends transit-encrypted envelopes to the network.

With strict typing, we no longer need strip_for_send. This handler only
accepts OutgoingTransitEnvelope which contains exactly what goes on the wire.
"""

# Removed core.types import
from protocols.quiet.protocol_types import OutgoingTransitEnvelope
from typing import Any, List, Callable, cast
import sqlite3
from core.handlers import Handler
from core import network as net


def filter_func(envelope: dict[str, Any]) -> bool:
    """
    Process envelopes that have transit encryption and destination info.
    
    The type system ensures we only get properly formatted transit envelopes.
    """
    # Symmetric DEM case
    if (
        'transit_ciphertext' in envelope and
        'transit_secret_id' in envelope and
        'dest_ip' in envelope and
        'dest_port' in envelope
    ):
        return True
    # Sealed KEM request case (0x02 + prekey_secret_id(16))
    if (
        'event_sealed' in envelope and
        'prekey_secret_id' in envelope and
        'dest_ip' in envelope and
        'dest_port' in envelope
    ):
        return True
    return False


def handler(envelope: dict[str, Any], send_func: Callable) -> None:
    """
    Send envelope to network.
    
    Args:
        envelope: Must be OutgoingTransitEnvelope type
        send_func: Framework-provided function to send data to network
        
    Returns:
        None - this is a terminal handler
    """
    # Build raw frame with optional 1-byte header
    # 0x01 = symmetric DEM (transit_secret_id[16] + ciphertext)
    # 0x02 = sealed KEM (prekey_secret_id[16] + sealed bytes)
    if 'transit_ciphertext' in envelope and 'transit_secret_id' in envelope:
        tsid_hex = envelope['transit_secret_id']
        try:
            id_bytes = bytes.fromhex(tsid_hex)
        except Exception:
            id_bytes = b""
        # transit_secret_id is 16 bytes event_id
        if len(id_bytes) != 16:
            id_bytes = id_bytes[:16].ljust(16, b'\0')
        raw_data = b"\x01" + id_bytes + envelope['transit_ciphertext']
    elif 'event_sealed' in envelope and 'prekey_secret_id' in envelope:
        pkid_hex = envelope['prekey_secret_id']
        try:
            pk_bytes = bytes.fromhex(pkid_hex)
        except Exception:
            pk_bytes = b""
        # prekey_secret_id is 16 bytes; exact length
        if len(pk_bytes) != 16:
            pk_bytes = pk_bytes[:16].ljust(16, b'\0')
        raw_data = b"\x02" + pk_bytes + envelope['event_sealed']
    else:
        raise TypeError("send_to_network: envelope missing required transit or sealed fields")
    
    # If we have a local-only secret hint, seed it into the simulator facade
    try:
        if 'transit_secret_hint' in envelope:
            from core import network as net
            secret_hint = envelope.get('transit_secret_hint')
            if isinstance(secret_hint, (bytes, bytearray)):
                vi = envelope.get('to_peer')
                net.seed_transit_secret_hint(
                    envelope['transit_secret_id'],
                    bytes(secret_hint),
                    envelope.get('network_id'),
                    vi
                )
                print(f"[send_to_network] Seeded transit hint for {envelope['transit_secret_id'][:8]}… viewer={vi}")
    except Exception:
        # Best-effort; ignore failures
        pass

    # Send to network (enqueue and respect due_ms)
    try:
        # Prefer module-level network facade if initialized
        if hasattr(net, 'enqueue_raw') and hasattr(net, 'has_simulator') and net.has_simulator():
            net.enqueue_raw(
                envelope['dest_ip'],
                envelope['dest_port'],
                raw_data,
                envelope.get('due_ms', 0)
            )
        else:
            # Fallback to provided send function
            send_func(
                envelope['dest_ip'],
                envelope['dest_port'],
                raw_data,
                envelope.get('due_ms', 0)
            )
    except Exception as e:
        # Log error but don't crash
        try:
            dip = envelope.get('dest_ip')
            dport = envelope.get('dest_port')
            print(f"Failed to send to {dip}:{dport}: {e}")
        except Exception:
            print(f"Failed to send: {e}")
    
    # No return - this is a terminal handler

class SendToNetworkHandler(Handler):
    """Handler for send to network."""

    @property
    def name(self) -> str:
        return "send_to_network"

    def filter(self, envelope: dict[str, Any]) -> bool:
        """Check if this handler should process the envelope."""
        return filter_func(envelope)

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """
        Terminal handler - sends to network and returns nothing.
        Note: In a real implementation, this would need access to a network send function.
        For now, we just log and return empty list.
        """
        # Send to simulator if initialized; fall back to log
        try:
            # Build raw frame inline with header
            # 0x01 = symmetric (transit_secret_id[16])
            # 0x02 = sealed (prekey_secret_id[16])
            if 'transit_ciphertext' in envelope and 'transit_secret_id' in envelope:
                tsid_hex = envelope['transit_secret_id']
                try:
                    id_bytes = bytes.fromhex(tsid_hex)
                except Exception:
                    id_bytes = b""
                if len(id_bytes) != 16:
                    id_bytes = id_bytes[:16].ljust(16, b'\0')
                raw = b"\x01" + id_bytes + envelope['transit_ciphertext']
            elif 'event_sealed' in envelope and 'prekey_secret_id' in envelope:
                pkid_hex = envelope['prekey_secret_id']
                try:
                    pk_bytes = bytes.fromhex(pkid_hex)
                except Exception:
                    pk_bytes = b""
                if len(pk_bytes) != 16:
                    pk_bytes = pk_bytes[:16].ljust(16, b'\0')
                raw = b"\x02" + pk_bytes + envelope['event_sealed']
            else:
                print(f"[send_to_network] ERROR: Missing required fields for transit or sealed send. Got: {envelope.keys()}")
                return []
            if hasattr(net, 'has_simulator') and net.has_simulator():
                # Seed hint if present on this envelope too (defensive)
                try:
                    if 'transit_secret_hint' in envelope:
                        vi = envelope.get('to_peer')
                        net.seed_transit_secret_hint(
                            envelope['transit_secret_id'],
                            envelope['transit_secret_hint'],
                            envelope.get('network_id'),
                            vi
                        )
                        print(f"[send_to_network] (process) Seeded transit hint for {envelope['transit_secret_id'][:8]}… viewer={vi}")
                except Exception:
                    pass
                net.enqueue_raw(
                    envelope['dest_ip'],
                    envelope['dest_port'],
                    raw,
                    envelope.get('due_ms', None)
                )
                print(f"[send_to_network] Queued raw to {envelope['dest_ip']}:{envelope['dest_port']}")
            else:
                print(f"[send_to_network] (no simulator) would send to {envelope['dest_ip']}:{envelope['dest_port']}")
        except Exception as e:
            print(f"[send_to_network] ERROR sending: {e}")
        return []
