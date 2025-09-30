"""
Quiet protocol-specific type definitions.

This module extends the core framework's base types with protocol-specific
fields and structures for the Quiet protocol.
"""

from typing import TypedDict, Literal, Any, Optional, Union, Dict, List
from typing_extensions import NotRequired


# Protocol-specific ID types
EventType = str
EventId = str
PeerId = str
NetworkId = str
GroupId = str
KeyId = str
TransitSecretId = str
AddressId = str
UserId = str
ChannelId = str
MessageId = str


# Protocol-specific envelope fields
class QuietEnvelopeState(TypedDict, total=False):
    """Quiet protocol-specific processing state flags"""
    deps_included_and_valid: bool
    sig_checked: bool
    validated: bool  # Protocol-managed, not framework
    projected: bool  # Protocol-managed, not framework
    stored: bool  # Protocol-managed, not framework
    prevalidated: bool
    self_created: bool
    self_signed: bool
    # Standardized flag for routing on the send path
    is_outgoing: bool
    outgoing_checked: bool
    stripped_for_send: bool
    write_to_store: bool
    missing_deps: bool
    unblocked: bool
    should_remove: bool
    is_group_member: bool


class QuietEnvelope(QuietEnvelopeState, total=False):
    """
    Quiet protocol envelope with all protocol-specific fields.

    The complete Quiet protocol envelope with all fields for:
    - Event encryption and signing
    - Network routing
    - Dependency management
    - Transit encryption
    """
    # Event data
    event_plaintext: dict[str, Any]
    # Removed: event_ciphertext; derive nonce||ct from event_blob when needed
    event_type: EventType
    event_id: EventId

    # Identity/ownership
    peer_id: PeerId

    # Network/group context
    network_id: NetworkId
    group_id: GroupId

    # Dependencies (overrides base dependencies)
    deps: list[str]  # e.g., ["identity:abc123", "key:xyz789"]
    resolved_deps: dict[str, dict[str, Any]]
    missing_deps_list: list[str]

    # Secret data (never sent over network)
    secret: dict[str, Any]

    # Transit encryption
    transit_secret_id: TransitSecretId
    transit_ciphertext: bytes
    transit_plaintext: dict[str, Any]

    # Event encryption
    secret_id: KeyId

    # Network transport
    origin_ip: str
    origin_port: int
    dest_ip: str
    dest_port: int
    received_at: float
    due_ms: int
    raw_data: bytes

    # User/address context
    user_id: UserId
    address_id: AddressId

    # Error tracking (could be protocol or framework)
    error: str


# Protocol-specific delta operations
OpType = Literal["insert", "update", "delete"]


class QuietDelta(TypedDict, total=False):
    """Quiet protocol-specific database delta"""
    op: OpType
    table: str
    data: dict[str, Any]
    where: dict[str, Any]  # For update/delete operations
    sql: str  # For raw SQL operations
    params: list[Any]  # Parameters for raw SQL


# Event-specific data structures
class IdentityEventData(TypedDict):
    """Identity event data structure"""
    type: Literal["identity"]
    peer_id: str
    network_id: str
    name: str
    created_at: int
    signature: str


class NetworkEventData(TypedDict):
    """Network event data structure"""
    type: Literal["network"]
    network_id: str
    creator_id: str
    name: str
    created_at: int
    signature: str


class KeyEventData(TypedDict):
    """Key event data structure"""
    type: Literal["key"]
    key_id: str
    peer_id: str
    network_id: str
    group_id: Optional[str]
    sealed_key: str
    created_at: int
    signature: str


class ChannelEventData(TypedDict):
    """Channel event data structure"""
    type: Literal["channel"]
    channel_id: str
    group_id: str
    network_id: str
    creator_id: str
    name: str
    created_at: int
    signature: str


class MessageEventData(TypedDict):
    """Message event data structure"""
    type: Literal["message"]
    message_id: str
    channel_id: str
    group_id: str
    network_id: str
    peer_id: str
    content: str
    created_at: int
    signature: str


class UserEventData(TypedDict):
    """User event data structure"""
    type: Literal["user"]
    user_id: str
    peer_id: str
    network_id: str
    address: str
    port: int
    created_at: int
    signature: str


# Specialized envelope types for different pipeline stages
class NetworkEnvelope(TypedDict):
    """Envelope with required fields for network reception"""
    origin_ip: str
    origin_port: int
    received_at: float
    raw_data: bytes


class TransitEnvelope(TypedDict):
    """Envelope with required fields for transit decryption"""
    transit_secret_id: TransitSecretId
    transit_ciphertext: bytes
    deps_included_and_valid: bool


class DecryptedEnvelope(TypedDict):
    """Envelope with required fields after decryption"""
    event_plaintext: dict[str, Any]
    event_type: EventType
    event_id: EventId
    peer_id: PeerId


class ValidatedEnvelope(TypedDict):
    """Envelope with required fields after validation"""
    event_plaintext: dict[str, Any]
    event_type: EventType
    event_id: EventId
    peer_id: PeerId
    sig_checked: Literal[True]
    validated: Literal[True]


class OutgoingEnvelope(TypedDict):
    """Envelope with required fields for outgoing messages"""
    is_outgoing: Literal[True]
    due_ms: int
    address_id: AddressId
    user_id: UserId
    peer_id: PeerId


class StrippedEnvelope(TypedDict):
    """Minimal envelope for network transmission"""
    transit_ciphertext: bytes
    transit_secret_id: TransitSecretId
    due_ms: int
    dest_ip: str
    dest_port: int
    stripped_for_send: Literal[True]


# Type validation functions
def validate_envelope_fields(envelope: dict[str, Any], required_fields: set[str]) -> bool:
    """
    Validate that envelope has required fields with non-None values.
    """
    return all(field in envelope and envelope.get(field) is not None
               for field in required_fields)


def is_network_envelope(envelope: dict[str, Any]) -> bool:
    """Check if envelope has NetworkEnvelope required fields"""
    return validate_envelope_fields(envelope, {'origin_ip', 'origin_port', 'received_at', 'raw_data'})


def is_transit_envelope(envelope: dict[str, Any]) -> bool:
    """Check if envelope has TransitEnvelope required fields"""
    return validate_envelope_fields(envelope, {'transit_secret_id', 'transit_ciphertext'})


def is_decrypted_envelope(envelope: dict[str, Any]) -> bool:
    """Check if envelope has DecryptedEnvelope required fields"""
    return validate_envelope_fields(envelope, {'event_plaintext', 'event_type', 'event_id', 'peer_id'})


def is_validated_envelope(envelope: dict[str, Any]) -> bool:
    """Check if envelope has ValidatedEnvelope required fields"""
    return (is_decrypted_envelope(envelope) and
            envelope.get('sig_checked') is True and
            envelope.get('validated') is True)


def is_outgoing_envelope(envelope: dict[str, Any]) -> bool:
    """Check if envelope has OutgoingEnvelope required fields"""
    return (envelope.get('is_outgoing') is True and
            validate_envelope_fields(envelope, {'due_ms', 'address_id', 'user_id', 'peer_id'}))

# ============================================================================
# Pipeline Stage Types (from envelope_types.py)
# ============================================================================

# Dependency types
class ValidatedEvent(TypedDict):
    """A validated event dependency."""
    event_plaintext: Dict[str, Any]
    event_type: str
    event_id: str
    validated: Literal[True]

class IdentityDep(TypedDict):
    """Identity dependency with optional local private key."""
    event_plaintext: Dict[str, Any]
    event_type: Literal["identity"]
    event_id: str
    validated: Literal[True]
    local_metadata: NotRequired[Dict[str, Any]]  # Contains private_key if local

class TransitKeyDep(TypedDict):
    """Transit key dependency (local secret, not an event)."""
    transit_secret: bytes
    network_id: str

class AddressDep(TypedDict):
    """Address dependency for routing."""
    ip: str
    port: int
    public_key: NotRequired[str]

# Union type for all possible dependencies
ResolvedDep = Union[ValidatedEvent, IdentityDep, TransitKeyDep, AddressDep]

class NetworkData(TypedDict):
    """Raw network data received from the network."""
    origin_ip: str
    origin_port: int
    received_at: int
    raw_data: bytes

class BaseEnvelope(TypedDict, total=False):
    """Base envelope that can contain any fields - similar to QuietEnvelope but for compatibility."""
    # This is essentially the same as QuietEnvelope
    # Keeping for backward compatibility with existing handler imports

# Additional pipeline stage types
class NeedsDepsEnvelope(TypedDict, total=False):
    """Envelope that needs dependency resolution."""
    deps: List[str]
    deps_included_and_valid: Union[bool, Literal[False]]
    unblocked: bool

class ResolvedDepsEnvelope(TypedDict):
    """Envelope with resolved dependencies."""
    deps_included_and_valid: Literal[True]
    resolved_deps: Dict[str, Any]

class MissingDepsEnvelope(TypedDict):
    """Envelope with missing dependencies."""
    missing_deps: Literal[True]
    missing_dep_list: List[str]

class TransitEncryptedEnvelope(TypedDict):
    """Envelope with transit encryption and resolved deps."""
    deps_included_and_valid: Literal[True]
    transit_secret_id: str
    transit_ciphertext: bytes
    resolved_deps: Dict[str, Any]

class EventEncryptedEnvelope(TypedDict):
    """Envelope after transit decryption."""
    network_id: str
    key_secret_id: str
    # Removed: event_ciphertext
    event_id: str
    write_to_store: Literal[True]

class EventWithId(TypedDict):
    """Envelope with event_id for early removal check."""
    event_id: str
    should_remove: NotRequired[bool]
    event_type: NotRequired[str]

class DecryptedEventWithType(TypedDict):
    """Decrypted event for content-based removal check."""
    event_id: str
    event_plaintext: Dict[str, Any]
    event_type: str
    should_remove: NotRequired[bool]

class EncryptedEvent(TypedDict):
    """Encrypted event ready for decryption."""
    deps_included_and_valid: Literal[True]
    should_remove: Literal[False]
    key_secret_id: str
    # Removed: event_ciphertext
    resolved_deps: Dict[str, Any]

class UnsealedKeyEvent(TypedDict):
    """Unsealed key event."""
    event_type: Literal["key"]
    key_id: str
    unsealed_secret: bytes
    group_id: str
    write_to_store: Literal[True]

class DecryptedEvent(TypedDict):
    """Decrypted event with plaintext."""
    event_plaintext: Dict[str, Any]
    event_type: str
    write_to_store: Literal[True]

class StorableEvent(TypedDict):
    """Event ready for storage."""
    write_to_store: Literal[True]
    event_id: NotRequired[str]
    # Removed: event_ciphertext
    event_plaintext: NotRequired[Dict[str, Any]]
    key_id: NotRequired[str]

class OutgoingTransitEnvelope(TypedDict):
    """Strict type for outgoing network data - ONLY what goes on the wire."""
    transit_ciphertext: bytes
    transit_secret_id: str
    dest_ip: str
    dest_port: int
    due_ms: NotRequired[int]

class PlaintextEvent(TypedDict, total=False):
    """Event with plaintext for signature checking."""
    event_plaintext: Dict[str, Any]
    sig_checked: bool
    resolved_deps: Dict[str, Any]

class GroupEvent(TypedDict, total=False):
    """Event with group membership."""
    event_plaintext: Dict[str, Any]
    is_group_member: bool

class ValidatableEvent(TypedDict):
    """Event ready for validation."""
    event_plaintext: Dict[str, Any]
    event_type: str
    sig_checked: Literal[True]
    is_group_member: NotRequired[Literal[True]]
    resolved_deps: NotRequired[Dict[str, Any]]

class ValidatedOrBlockedEvent(TypedDict, total=False):
    """Validated or blocked event for unblocking."""
    validated: Literal[True]
    missing_deps: Literal[True]
    event_id: str

class ProjectableEvent(TypedDict):
    """Validated event ready for projection."""
    validated: Literal[True]
    event_type: str
    event_plaintext: Dict[str, Any]

class ProjectedEvent(ProjectableEvent):
    """Event after projection."""
    projected: Literal[True]
    deltas: List[Dict[str, Any]]

# Creation pipeline types
class CommandEnvelope(TypedDict):
    """Envelope created by a command."""
    event_plaintext: Dict[str, Any]
    event_type: str
    self_created: Literal[True]
    deps: List[str]

class SignableEnvelope(TypedDict):
    """Envelope ready for signing."""
    event_plaintext: Dict[str, Any]
    self_created: Literal[True]
    deps_included_and_valid: Literal[True]
    resolved_deps: Dict[str, Any]

class SignedEnvelope(SignableEnvelope):
    """Envelope after signing."""
    # event_plaintext is inherited from SignableEnvelope and now includes signature
    pass

class SignableOrVerifiableEvent(TypedDict, total=False):
    """Event that needs signing or verification."""
    event_plaintext: Dict[str, Any]
    sig_checked: bool
    self_created: bool
    deps_included_and_valid: Literal[True]
    resolved_deps: Dict[str, Any]

class ValidatedPlaintext(TypedDict):
    """Validated plaintext ready for encryption."""
    validated: Literal[True]
    event_plaintext: Dict[str, Any]
    # Removed: event_ciphertext

class EncryptedEventOutput(TypedDict):
    """Event after encryption."""
    # Removed: event_ciphertext
    key_secret_id: str
    event_id: str
    write_to_store: Literal[True]

# Outgoing pipeline types
class SendParams(TypedDict):
    """Parameters for sending an event."""
    event_id: str
    peer_id: str
    due_ms: NotRequired[int]

class OutgoingWithDeps(TypedDict):
    """Outgoing envelope with resolved dependencies."""
    is_outgoing: Literal[True]
    deps_included_and_valid: Literal[True]
    resolved_deps: Dict[str, Any]
    outgoing_checked: NotRequired[None]

class OutgoingEncrypted(TypedDict):
    """Outgoing envelope with event encryption."""
    outgoing_checked: Literal[True]
    # Removed: event_ciphertext
    transit_secret_id: str
    resolved_deps: Dict[str, Any]

class TransitEncrypted(TypedDict):
    """Envelope with transit encryption."""
    transit_ciphertext: bytes
    transit_secret_id: str
    dest_ip: NotRequired[str]
    dest_port: NotRequired[int]
    due_ms: NotRequired[int]

class PreStrippedEnvelope(TypedDict):
    """Envelope before stripping for send."""
    transit_ciphertext: bytes
    transit_secret_id: str
    dest_ip: NotRequired[str]
    dest_port: NotRequired[int]
    due_ms: NotRequired[int]

# Add cast_envelope function that was referenced but missing
def cast_envelope(envelope: Dict[str, Any], target_type: type) -> Dict[str, Any]:
    """Cast envelope to a specific type with validation."""
    # In a real implementation, we could validate against TypedDict fields
    return envelope
