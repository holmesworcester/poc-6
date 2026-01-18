"""Type scaffolding for projection v2 (pure functional projectors)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

DepSource = Literal["table", "value", "context"]


class DepSpec(TypedDict, total=False):
    source: DepSource
    key: str
    table: str
    fields: list[str]
    value: Any
    key_from: str
    required_if_present: bool


class SignerSpec(TypedDict, total=False):
    id_field: str
    type_field: str


class EventSpec(TypedDict, total=False):
    encrypted: bool
    signer: SignerSpec
    requires: dict[str, DepSpec]
    optional: dict[str, DepSpec]
    cascade_on_delete: list[str]


@dataclass(frozen=True)
class ProjectionContext:
    event_id: str
    event_type: str
    event_data: dict[str, Any]
    recorded_by: str
    recorded_at: int
    deps: dict[str, Any]
    signer: dict[str, Any] | None


@dataclass(frozen=True)
class WriteOp:
    op: Literal["insert", "update", "delete"]
    table: str
    values: dict[str, Any]
    where: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmitEvent:
    """Deterministic event to be created by projection.

    Some projectors must emit new events (e.g., group_key_shared emits group_key).
    These must be deterministic - same input produces same event.
    The apply layer handles creation/storage of emitted events.
    """
    event_type: str
    event_data: dict[str, Any]
    # Optional: specify the peer_id to use for signing/storing
    # If None, uses recorded_by from projection context
    peer_id: str | None = None


@dataclass(frozen=True)
class Command:
    """Side effect command to be executed after projection.

    Commands are non-deterministic side effects that projectors need to trigger
    but cannot execute themselves (since project_pure must be pure).

    The apply layer executes these after writes are committed.
    Command handlers are registered by command_type.

    Example: send_ack command for connection_request projection
    """
    command_type: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ProjectorResult:
    writes: tuple[WriteOp, ...]
    valid_event: bool = True
    emit_events: tuple[EmitEvent, ...] = ()
    commands: tuple[Command, ...] = ()


@dataclass(frozen=True)
class ResolveResult:
    status: Literal["ok", "block", "reject"]
    ctx: ProjectionContext | None
    missing: tuple[str, ...] = ()
    error: str | None = None
