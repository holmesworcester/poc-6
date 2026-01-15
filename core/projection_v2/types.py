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
class ProjectorResult:
    writes: tuple[WriteOp, ...]
    valid_event: bool = True


@dataclass(frozen=True)
class ResolveResult:
    status: Literal["ok", "block", "reject"]
    ctx: ProjectionContext | None
    missing: tuple[str, ...] = ()
    error: str | None = None
