"""File slice projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

File slices are NOT signed - integrity is verified via root_hash at the
file level (in message_attachment). This is intentional for performance.
"""

from typing import TypedDict
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class FileSliceEventData(TypedDict):
    type: str
    file_id: str
    slice_number: int
    nonce: str  # base64
    ciphertext: str  # base64
    poly_tag: str  # base64
    signed_by: str
    created_at: int


class FileSliceInput(TypedDict):
    event_id: str
    event_data: FileSliceEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plain JSON (not group-wrapped)
    "signer_type": "none",  # NOT signed - integrity via root_hash
    "dependencies": [],  # No blocking dependencies
    "tables": ["file_slices", "event_dependencies"],
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: FileSliceInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Outputs file_slices row and event_dependencies row (slice depends on file).
    """
    import crypto

    event_id = input_dict["event_id"]
    event_data: FileSliceEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    file_id = event_data.get("file_id")
    slice_number = event_data.get("slice_number")
    nonce_b64 = event_data.get("nonce")
    ciphertext_b64 = event_data.get("ciphertext")
    poly_tag_b64 = event_data.get("poly_tag")

    if not all([file_id, slice_number is not None, nonce_b64, ciphertext_b64, poly_tag_b64]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Decode from base64
    nonce = crypto.b64decode(nonce_b64)
    ciphertext = crypto.b64decode(ciphertext_b64)
    poly_tag = crypto.b64decode(poly_tag_b64)

    # Output: file_slices row
    slice_row = {
        "file_id": file_id,
        "slice_number": slice_number,
        "nonce": nonce,
        "ciphertext": ciphertext,
        "poly_tag": poly_tag,
        "event_id": event_id,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    # Output: event_dependencies row (slice depends on file)
    dep_row = {
        "child_event_id": event_id,
        "parent_event_id": file_id,
        "recorded_by": recorded_by,
        "dependency_type": "file",
    }

    return ProjectorResult(
        valid=True,
        tables={
            "file_slices": [slice_row],
            "event_dependencies": [dep_row],
        },
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    file_id: str = "file_123",
    slice_number: int = 0,
    nonce: str = "AAAAAAAAAAAAAAAAAAAAAA==",  # 16 bytes base64
    ciphertext: str = "SGVsbG8gV29ybGQ=",  # "Hello World" base64
    poly_tag: str = "AAAAAAAAAAAAAAAAAAAAAA==",  # 16 bytes base64
    signed_by: str = "peer_shared_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "file_slice",
        "file_id": file_id,
        "slice_number": slice_number,
        "nonce": nonce,
        "ciphertext": ciphertext,
        "poly_tag": poly_tag,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "slice_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }
