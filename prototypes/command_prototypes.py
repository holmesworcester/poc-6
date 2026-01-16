from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class DepSpec:
    name: str
    source: str  # "table" | "context" | "value"
    table: str | None = None
    key_from: str | None = None  # e.g. "args.channel_id", "inputs.peer_self.user_id"
    fields: tuple[str, ...] | None = None
    value: Any | None = None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    requires: tuple[DepSpec, ...]
    optional: tuple[DepSpec, ...]


@dataclass(frozen=True)
class ResolveResult:
    inputs: dict[str, Any]
    blocked: bool
    missing: tuple[str, ...]


@dataclass(frozen=True)
class PlannedEvent:
    event_type: str
    event_data: dict[str, Any]
    event_id: str


@dataclass(frozen=True)
class CreatePlan:
    events: tuple[PlannedEvent, ...]


class FakeDB:
    def __init__(self, tables: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._tables = tables

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        return self._tables.get(table, {}).get(key)


class QueryAPI:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        return self._db.get("channels", channel_id)

    def get_peer_self(self, peer_id: str) -> dict[str, Any] | None:
        return self._db.get("peer_self", peer_id)

    def get_user_name(self, user_id: str) -> dict[str, Any] | None:
        return self._db.get("user_names", user_id)

    def get_removed_user(self, user_id: str) -> dict[str, Any] | None:
        return self._db.get("removed_users", user_id)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        return self._db.get("messages", message_id)


def _resolve_path(path: str | None, args: dict[str, Any], ctx: dict[str, Any], inputs: dict[str, Any]) -> Any:
    if not path:
        return None
    parts = path.split(".")
    if not parts:
        return None
    root = parts[0]
    if root == "args":
        value: Any = args
    elif root == "context":
        value = ctx
    elif root == "inputs":
        value = inputs
    else:
        raise ValueError(f"unknown root in path: {path}")
    for part in parts[1:]:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
    return value


def resolve_inputs(spec: CommandSpec, args: dict[str, Any], ctx: dict[str, Any], db: FakeDB) -> ResolveResult:
    inputs: dict[str, Any] = {}
    pending = list(spec.requires) + list(spec.optional)
    missing: list[str] = []

    progressed = True
    while pending and progressed:
        progressed = False
        for dep in list(pending):
            if dep.source == "value":
                inputs[dep.name] = dep.value
                pending.remove(dep)
                progressed = True
                continue
            if dep.source == "context":
                inputs[dep.name] = _resolve_path(dep.key_from, args, ctx, inputs)
                pending.remove(dep)
                progressed = True
                continue
            if dep.source != "table":
                raise ValueError(f"unknown dep source: {dep.source}")

            key = _resolve_path(dep.key_from, args, ctx, inputs)
            if key is None:
                continue
            row = db.get(dep.table or "", key)
            if not row:
                if dep in spec.requires:
                    missing.append(dep.name)
                inputs[dep.name] = None
            else:
                if dep.fields:
                    inputs[dep.name] = {field: row.get(field) for field in dep.fields}
                else:
                    inputs[dep.name] = dict(row)
            pending.remove(dep)
            progressed = True

    for dep in pending:
        if dep in spec.requires:
            missing.append(dep.name)
        inputs[dep.name] = None

    return ResolveResult(inputs=inputs, blocked=bool(missing), missing=tuple(sorted(set(missing))))


def _event_id_for(data: dict[str, Any]) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_plan_send_message(inputs: dict[str, Any], args: dict[str, Any], ctx: dict[str, Any]) -> CreatePlan:
    if inputs.get("removed_user"):
        raise ValueError("user removed")
    if not inputs.get("user_name"):
        raise ValueError("missing username")

    channel = inputs["channel"]
    peer_self = inputs["peer_self"]
    event_data = {
        "type": "message",
        "channel_id": args["channel_id"],
        "signed_by": peer_self["peer_shared_id"],
        "author_id": peer_self["user_id"],
        "content": args["content"],
        "created_at": ctx["now_ms"],
        "disappearing_time_ms": channel["disappearing_time_ms"],
    }
    planned = PlannedEvent(
        event_type="message",
        event_data=event_data,
        event_id=_event_id_for(event_data),
    )
    return CreatePlan(events=(planned,))


def create_message_imperative(args: dict[str, Any], ctx: dict[str, Any], api: QueryAPI) -> CreatePlan:
    channel = api.get_channel(args["channel_id"])
    if not channel:
        raise ValueError("channel missing")

    peer_self = api.get_peer_self(ctx["peer_id"])
    if not peer_self:
        raise ValueError("peer_self missing")

    if api.get_removed_user(peer_self["user_id"]):
        raise ValueError("user removed")
    if not api.get_user_name(peer_self["user_id"]):
        raise ValueError("missing username")

    event_data = {
        "type": "message",
        "channel_id": args["channel_id"],
        "signed_by": peer_self["peer_shared_id"],
        "author_id": peer_self["user_id"],
        "content": args["content"],
        "created_at": ctx["now_ms"],
        "disappearing_time_ms": channel["disappearing_time_ms"],
    }
    planned = PlannedEvent(
        event_type="message",
        event_data=event_data,
        event_id=_event_id_for(event_data),
    )
    return CreatePlan(events=(planned,))


SLICE_SIZE = 4


def _slice_bytes(blob: bytes, size: int) -> list[bytes]:
    return [blob[offset:offset + size] for offset in range(0, len(blob), size)]


def create_plan_attachment(inputs: dict[str, Any], args: dict[str, Any], ctx: dict[str, Any]) -> CreatePlan:
    peer_self = inputs["peer_self"]
    slices = _slice_bytes(args["file_data"], SLICE_SIZE)

    slice_payloads = [hashlib.sha256(plaintext).digest() for plaintext in slices]
    file_id = hashlib.sha256(b"".join(slice_payloads)).hexdigest()
    root_hash = hashlib.sha256(b"".join(slice_payloads)).digest()

    planned_events: list[PlannedEvent] = []
    for idx, ciphertext in enumerate(slice_payloads):
        event_data = {
            "type": "file_slice",
            "file_id": file_id,
            "slice_number": idx,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "created_at": ctx["now_ms"],
        }
        planned_events.append(PlannedEvent(
            event_type="file_slice",
            event_data=event_data,
            event_id=_event_id_for(event_data),
        ))

    attachment_data = {
        "type": "message_attachment",
        "message_id": args["message_id"],
        "file_id": file_id,
        "filename": args.get("filename"),
        "mime_type": args.get("mime_type"),
        "blob_bytes": len(args["file_data"]),
        "root_hash": base64.b64encode(root_hash).decode("ascii"),
        "total_slices": len(planned_events),
        "signed_by": peer_self["peer_shared_id"],
        "created_at": ctx["now_ms"],
    }
    planned_events.append(PlannedEvent(
        event_type="message_attachment",
        event_data=attachment_data,
        event_id=_event_id_for(attachment_data),
    ))

    return CreatePlan(events=tuple(planned_events))


def create_plan_reaction(inputs: dict[str, Any], args: dict[str, Any], ctx: dict[str, Any]) -> CreatePlan:
    message = inputs["message"]
    if not message:
        raise ValueError("message missing")
    peer_self = inputs["peer_self"]
    global_count = inputs.get("global_count")
    if global_count is None:
        raise ValueError("global_count missing")

    event_data = {
        "type": "message_reaction",
        "message_id": args["message_id"],
        "reactor_id": peer_self["user_id"],
        "signed_by": peer_self["peer_shared_id"],
        "emoji": args["emoji"],
        "created_at": ctx["now_ms"],
        "global_count": global_count,
    }
    planned = PlannedEvent(
        event_type="message_reaction",
        event_data=event_data,
        event_id=_event_id_for(event_data),
    )
    return CreatePlan(events=(planned,))


def create_reaction_imperative(args: dict[str, Any], ctx: dict[str, Any], api: QueryAPI) -> CreatePlan:
    message = api.get_message(args["message_id"])
    if not message:
        raise ValueError("message missing")

    peer_self = api.get_peer_self(ctx["peer_id"])
    if not peer_self:
        raise ValueError("peer_self missing")

    event_data = {
        "type": "message_reaction",
        "message_id": args["message_id"],
        "reactor_id": peer_self["user_id"],
        "signed_by": peer_self["peer_shared_id"],
        "emoji": args["emoji"],
        "created_at": ctx["now_ms"],
        "global_count": ctx["global_count"],
    }
    planned = PlannedEvent(
        event_type="message_reaction",
        event_data=event_data,
        event_id=_event_id_for(event_data),
    )
    return CreatePlan(events=(planned,))


MESSAGE_CMD_SPEC = CommandSpec(
    name="send_message",
    requires=(
        DepSpec(
            name="channel",
            source="table",
            table="channels",
            key_from="args.channel_id",
            fields=("group_id", "disappearing_time_ms"),
        ),
        DepSpec(
            name="peer_self",
            source="table",
            table="peer_self",
            key_from="context.peer_id",
            fields=("peer_shared_id", "user_id"),
        ),
        DepSpec(
            name="user_name",
            source="table",
            table="user_names",
            key_from="inputs.peer_self.user_id",
            fields=("name",),
        ),
    ),
    optional=(
        DepSpec(
            name="removed_user",
            source="table",
            table="removed_users",
            key_from="inputs.peer_self.user_id",
            fields=("removed_at",),
        ),
    ),
)


ATTACHMENT_CMD_SPEC = CommandSpec(
    name="attach_file",
    requires=(
        DepSpec(
            name="message",
            source="table",
            table="messages",
            key_from="args.message_id",
            fields=("group_id",),
        ),
        DepSpec(
            name="peer_self",
            source="table",
            table="peer_self",
            key_from="context.peer_id",
            fields=("peer_shared_id",),
        ),
    ),
    optional=(),
)


REACTION_CMD_SPEC = CommandSpec(
    name="add_reaction",
    requires=(
        DepSpec(
            name="message",
            source="table",
            table="messages",
            key_from="args.message_id",
            fields=("group_id",),
        ),
        DepSpec(
            name="peer_self",
            source="table",
            table="peer_self",
            key_from="context.peer_id",
            fields=("peer_shared_id", "user_id"),
        ),
        DepSpec(
            name="global_count",
            source="context",
            key_from="context.global_count",
        ),
    ),
    optional=(),
)


if __name__ == "__main__":
    fake_db = FakeDB(
        {
            "channels": {
                "chan-1": {"group_id": "grp-1", "disappearing_time_ms": 0},
            },
            "peer_self": {
                "peer-1": {"peer_shared_id": "ps-1", "user_id": "user-1"},
            },
            "user_names": {
                "user-1": {"name": "alice"},
            },
            "messages": {
                "msg-1": {"group_id": "grp-1"},
            },
        }
    )

    ctx = {"peer_id": "peer-1", "now_ms": 123456, "global_count": 7}
    api = QueryAPI(fake_db)
    args = {"channel_id": "chan-1", "content": "hi"}
    resolved = resolve_inputs(MESSAGE_CMD_SPEC, args, ctx, fake_db)
    print("send_message blocked=", resolved.blocked, "missing=", resolved.missing)
    plan = create_plan_send_message(resolved.inputs, args, ctx)
    print("send_message events=", len(plan.events))
    plan = create_message_imperative(args, ctx, api)
    print("send_message imperative events=", len(plan.events))

    file_args = {"message_id": "msg-1", "file_data": b"hello world", "filename": "a.txt"}
    resolved_file = resolve_inputs(ATTACHMENT_CMD_SPEC, file_args, ctx, fake_db)
    print("attach_file blocked=", resolved_file.blocked, "missing=", resolved_file.missing)
    plan_file = create_plan_attachment(resolved_file.inputs, file_args, ctx)
    print("attach_file events=", len(plan_file.events))

    reaction_args = {"message_id": "msg-1", "emoji": ":+1:"}
    resolved_reaction = resolve_inputs(REACTION_CMD_SPEC, reaction_args, ctx, fake_db)
    print("reaction blocked=", resolved_reaction.blocked, "missing=", resolved_reaction.missing)
    plan_reaction = create_plan_reaction(resolved_reaction.inputs, reaction_args, ctx)
    print("reaction events=", len(plan_reaction.events))
    plan_reaction = create_reaction_imperative(reaction_args, ctx, api)
    print("reaction imperative events=", len(plan_reaction.events))
