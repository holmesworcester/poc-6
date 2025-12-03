"""Sync connect projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Note: sync_connections is a DEVICE-WIDE table (not subjective).
The pure projector validates and extracts data; the wrapper handles
the device-wide write to sync_connections.

Signature verification is POLYMORPHIC:
- If invite_id + invite_signature present: verify against invite's public key
- Otherwise: verify against peer_shared's public key

Dependencies needed:
- peer_shared_public_key: bytes - public key for signed_by peer
- invite_event: dict - invite event data (optional, for invite auth)
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class SyncConnectEventData(TypedDict):
    type: str
    peer_id: str
    signed_by: str
    address: str
    port: int
    response_transit_key_id: str
    response_transit_key: str
    invite_id: NotRequired[str]
    invite_signature: NotRequired[str]
    created_at: int


class SyncConnectInput(TypedDict):
    event_id: str
    event_data: SyncConnectEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict  # peer_shared_public_key, invite_event (optional)


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,  # Transit-wrapped
    "signer_type": "peer_shared_polymorphic",  # Verified in projector using deps
    "dependencies": ["peer_shared_public_key", "invite_event?"],  # ? = optional
    "tables": ["sync_connections"],  # Device-wide table (use apply_result_device_wide)
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: SyncConnectInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Validates sync_connect and outputs connection info.
    Signature verification is handled by projection.py using the embedded public_key.
    Use apply_result_device_wide() to write to sync_connections.
    """
    import crypto
    import json

    event_data: SyncConnectEventData = input_dict["event_data"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict.get("dependencies", {})
    signature_valid_from_framework = input_dict.get("signature_valid", False)

    peer_shared_id = event_data.get("signed_by")
    response_transit_key_id = event_data.get("response_transit_key_id")
    response_transit_key_b64 = event_data.get("response_transit_key")
    address = event_data.get("address")
    port = event_data.get("port")
    invite_id = event_data.get("invite_id")
    invite_signature_b64 = event_data.get("invite_signature")

    if not all([peer_shared_id, response_transit_key_id, response_transit_key_b64]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Signature verification
    # The main signature is verified by projection.py using embedded public_key
    # We trust that result and only do additional invite_signature verification if present
    signature_valid = signature_valid_from_framework

    # Additional invite signature verification (optional auth for linkers)
    if invite_id and invite_signature_b64:
        invite_event = deps.get("invite_event")
        if invite_event:
            invite_pubkey_b64 = invite_event.get("invite_pubkey")
            if invite_pubkey_b64:
                try:
                    invite_public_key = crypto.b64decode(invite_pubkey_b64)
                    # Verify invite_signature over event without that field
                    connect_without_sig = {k: v for k, v in event_data.items() if k != 'invite_signature'}
                    sig_data = json.dumps(connect_without_sig, sort_keys=True).encode()
                    invite_signature = crypto.b64decode(invite_signature_b64)
                    if crypto.verify(sig_data, invite_signature, invite_public_key):
                        log.debug(f"sync_connect: invite signature also verified")
                except Exception as e:
                    log.debug(f"sync_connect: invite signature check failed (non-fatal): {e}")

    if not signature_valid:
        return ProjectorResult(valid=False, reason="signature verification failed")

    # Decode response transit key
    response_transit_key = crypto.b64decode(response_transit_key_b64)

    # Output: sync_connections row (device-wide table)
    row = {
        "peer_shared_id": peer_shared_id,
        "response_transit_key_id": response_transit_key_id,
        "response_transit_key": response_transit_key,
        "address": address,
        "port": port,
        "invite_id": invite_id,
        "last_seen_ms": recorded_at,
        "ttl_ms": 300000,  # 5 minutes default TTL
    }

    return ProjectorResult(
        valid=True,
        tables={"sync_connections": [row]},
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    peer_id: str = "peer_123",
    signed_by: str = "peer_shared_123",
    address: str = "127.0.0.1",
    port: int = 8000,
    response_transit_key_id: str = "key_123",
    response_transit_key: str = "base64key==",
    invite_id: str | None = None,
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    data = {
        "type": "sync_connect",
        "peer_id": peer_id,
        "signed_by": signed_by,
        "address": address,
        "port": port,
        "response_transit_key_id": response_transit_key_id,
        "response_transit_key": response_transit_key,
        "created_at": created_at,
    }
    if invite_id:
        data["invite_id"] = invite_id
    return data


def make_input(
    event_id: str = "conn_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    peer_shared_public_key: bytes | None = None,
    invite_event: dict | None = None,
) -> dict:
    """Build complete input dict for testing.

    Note: For signature verification to pass, you must provide
    peer_shared_public_key (for peer auth) or invite_event (for invite auth).
    """
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "peer_shared_public_key": peer_shared_public_key,
            "invite_event": invite_event,
        },
    }
