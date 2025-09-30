"""
Handler that processes envelopes from the network interface.
Extracts transit layer information from raw network data.
"""
import time
from typing import List, Dict, Any
import sqlite3
from core.handlers import Handler
from core.crypto import hash
from protocols.quiet.protocol_types import validate_envelope_fields, cast_envelope
# TODO: Handle imports: NetworkEnvelope, TransitEnvelope


class ReceiveFromNetworkHandler(Handler):
    """
    Processes raw network data and extracts transit layer information.
    Consumes: envelopes with origin_ip, origin_port, received_at, raw_data
    Emits: envelopes with transit_key_id and transit_ciphertext
    """
    
    @property
    def name(self) -> str:
        return "receive_from_network"
    
    def filter(self, envelope: dict[str, Any]) -> bool:
        """Process envelopes with raw network data."""
        # Validate we have NetworkEnvelope required fields
        return (
            validate_envelope_fields(envelope, {'raw_data', 'origin_ip', 'origin_port', 'received_at'}) and
            envelope.get('transit_secret_id') is None  # Not yet processed
        )
    
    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Extract transit key ID and ciphertext from raw data."""
        # Runtime validation - ensure we have required fields
        if not validate_envelope_fields(envelope, {'raw_data', 'origin_ip', 'origin_port', 'received_at'}):
            envelope['error'] = "Missing required NetworkEnvelope fields"
            return []
        
        raw_data = envelope['raw_data']
        if not isinstance(raw_data, (bytes, bytearray)) or len(raw_data) < 32:
            envelope['error'] = "Raw data too short for transit layer"
            return []
        # Detect optional 1-byte header: 0x01 = symmetric transit; 0x02 = sealed
        header = None
        idx = 0
        if len(raw_data) >= 33 and raw_data[0] in (1, 2):
            header = raw_data[0]
            idx = 1
        # Determine id length by header: both 0x01 (transit) and 0x02 (sealed) use 16-byte ids (event_ids)
        # Fallback for legacy frames without header assumes 32-byte id for transit.
        id_len = 16 if header in (1, 2) else 32
        id_bytes = raw_data[idx:idx+id_len]
        payload = raw_data[idx+id_len:]

        # Sealed KEM request
        if header == 2:
            prekey_secret_id = id_bytes.hex()
            sealed_env: dict[str, Any] = {
                'origin_ip': envelope['origin_ip'],
                'origin_port': envelope['origin_port'],
                'received_at': envelope['received_at'],
                'event_sealed': payload,
                'prekey_secret_id': prekey_secret_id,
                'deps': [prekey_secret_id],
            }
            return [sealed_env]

        # Symmetric DEM transit (header=1 or legacy headerless)
        transit_secret_id_hex = id_bytes.hex()
        new_envelope: dict[str, Any] = {
            'origin_ip': envelope['origin_ip'],
            'origin_port': envelope['origin_port'],
            'received_at': envelope['received_at'],
            'transit_secret_id': transit_secret_id_hex,
            'transit_ciphertext': payload,
            'deps': [transit_secret_id_hex]
        }
        try:
            from core import network as net
            if hasattr(net, 'get_transit_secret_hint'):
                hint = net.get_transit_secret_hint(transit_secret_id_hex)
                if hint:
                    # Support 2 or 3-tuple hints
                    secret_bytes = hint[0]
                    network_id = hint[1] if len(hint) >= 2 else None
                    viewer_identity = hint[2] if len(hint) >= 3 else None
                    new_envelope['resolved_deps'] = {
                        f'transit_secret:{transit_secret_id_hex}': {
                            'transit_secret': secret_bytes,
                            'network_id': network_id or ''
                        }
                    }
                    new_envelope['deps_included_and_valid'] = True
                    print(f"[receive_from_network] Attached transit hint for {transit_secret_id_hex[:8]}…")
                    if viewer_identity:
                        new_envelope['to_peer'] = viewer_identity
        except Exception:
            pass
        return [new_envelope]
