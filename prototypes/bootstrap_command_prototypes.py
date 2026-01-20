from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class PlannedEvent:
    event_type: str
    event_data: dict[str, Any]
    event_id: str
    deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreatePlan:
    events: tuple[PlannedEvent, ...]
    notes: tuple[str, ...] = ()


class FakeDB:
    def __init__(self, tables: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._tables = tables

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        return self._tables.get(table, {}).get(key)


class QueryAPI:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def peer_exists(self, peer_id: str) -> bool:
        return self._db.get("local_peers", peer_id) is not None

    def has_main_group_key(self, peer_id: str) -> bool:
        row = self._db.get("group_keys", peer_id)
        return bool(row and row.get("key_id"))

    def get_network_id(self, peer_id: str) -> str | None:
        row = self._db.get("networks", peer_id)
        return row["network_id"] if row else None


def _event_id_for(data: dict[str, Any]) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _derive(seed: str, label: str) -> str:
    return hashlib.sha256(f"{seed}:{label}".encode("utf-8")).hexdigest()[:16]


# -----------------------
# Pure bootstrap planning
# -----------------------

def plan_new_network_pure(args: dict[str, Any], ctx: dict[str, Any]) -> CreatePlan:
    """Pure plan: no DB queries, all randomness provided via ctx.seed."""
    seed = ctx["seed"]
    now_ms = ctx["now_ms"]

    peer_private = _derive(seed, "peer_private")
    peer_public = _derive(seed, "peer_public")
    network_private = _derive(seed, "network_private")
    network_public = _derive(seed, "network_public")
    invite_private = _derive(seed, "invite_private")
    invite_public = _derive(seed, "invite_public")

    events: list[PlannedEvent] = []

    peer_data = {
        "type": "peer",
        "public_key": peer_public,
        "private_key": peer_private,
        "created_at": now_ms,
    }
    peer_id = _event_id_for(peer_data)
    events.append(PlannedEvent("peer", peer_data, peer_id))

    network_data = {
        "type": "network",
        "network_pubkey": network_public,
        "created_at": now_ms,
    }
    network_id = _event_id_for(network_data)
    events.append(PlannedEvent("network", network_data, network_id))

    invite_data = {
        "type": "invite",
        "mode": "user",
        "network_id": network_id,
        "invite_pubkey": invite_public,
        "signed_by": network_id,
        "created_at": now_ms,
    }
    invite_id = _event_id_for(invite_data)
    events.append(PlannedEvent("invite", invite_data, invite_id, deps=(network_id,)))

    user_data = {
        "type": "user",
        "name": args["username"],
        "invite_id": invite_id,
        "signed_by": invite_id,
        "created_at": now_ms,
    }
    user_id = _event_id_for(user_data)
    events.append(PlannedEvent("user", user_data, user_id, deps=(invite_id,)))

    peer_invite_data = {
        "type": "invite",
        "mode": "peer",
        "user_id": user_id,
        "invite_pubkey": _derive(seed, "peer_invite_public"),
        "signed_by": user_id,
        "created_at": now_ms,
    }
    peer_invite_id = _event_id_for(peer_invite_data)
    events.append(PlannedEvent("invite", peer_invite_data, peer_invite_id, deps=(user_id,)))

    peer_shared_data = {
        "type": "peer_shared",
        "peer_id": peer_id,
        "user_id": user_id,
        "device_name": args["device_name"],
        "signed_by": peer_invite_id,
        "created_at": now_ms,
    }
    peer_shared_id = _event_id_for(peer_shared_data)
    events.append(PlannedEvent("peer_shared", peer_shared_data, peer_shared_id, deps=(peer_invite_id,)))

    admin_data = {
        "type": "admin",
        "user_id": user_id,
        "network_id": network_id,
        "signed_by": network_id,
        "created_at": now_ms,
    }
    admin_id = _event_id_for(admin_data)
    events.append(PlannedEvent("admin", admin_data, admin_id, deps=(network_id, user_id)))

    group_data = {
        "type": "group",
        "name": "all_users",
        "network_id": network_id,
        "is_main": True,
        "signed_by": network_id,
        "created_at": now_ms,
    }
    group_id = _event_id_for(group_data)
    events.append(PlannedEvent("group", group_data, group_id, deps=(network_id,)))

    channel_data = {
        "type": "channel",
        "name": "general",
        "group_id": group_id,
        "admin_grant": admin_id,
        "signed_by": peer_shared_id,
        "created_at": now_ms,
    }
    channel_id = _event_id_for(channel_data)
    events.append(PlannedEvent("channel", channel_data, channel_id, deps=(group_id, admin_id)))

    member_data = {
        "type": "group_member",
        "group_id": group_id,
        "user_id": user_id,
        "admin_grant": admin_id,
        "signed_by": peer_shared_id,
        "created_at": now_ms,
    }
    member_id = _event_id_for(member_data)
    events.append(PlannedEvent("group_member", member_data, member_id, deps=(group_id, admin_id)))

    username_data = {
        "type": "username_update",
        "user_id": user_id,
        "name": args["username"],
        "signed_by": peer_shared_id,
        "created_at": now_ms,
    }
    username_id = _event_id_for(username_data)
    events.append(PlannedEvent("username_update", username_data, username_id, deps=(group_id,)))

    if args.get("network_name"):
        network_name_data = {
            "type": "network_name_update",
            "network_id": network_id,
            "name": args["network_name"],
            "signed_by": peer_shared_id,
            "created_at": now_ms,
        }
        network_name_id = _event_id_for(network_name_data)
        events.append(PlannedEvent("network_name_update", network_name_data, network_name_id, deps=(group_id,)))

    notes = (
        f"secrets: network_private={network_private}, invite_private={invite_private}",
    )
    return CreatePlan(events=tuple(events), notes=notes)


def plan_join_pure(args: dict[str, Any], ctx: dict[str, Any]) -> CreatePlan:
    """Pure plan for join with explicit inputs (invite_data + key availability)."""
    invite = args["invite_data"]
    now_ms = ctx["now_ms"]
    events: list[PlannedEvent] = []
    notes: list[str] = []

    prekey_data = {
        "type": "group_prekey",
        "invite_id": invite["invite_id"],
        "created_at": now_ms,
    }
    prekey_id = _event_id_for(prekey_data)
    events.append(PlannedEvent("group_prekey", prekey_data, prekey_id))

    invite_accepted = {
        "type": "invite_accepted",
        "invite_id": invite["invite_id"],
        "invite_prekey_id": invite["invite_prekey_id"],
        "created_at": now_ms,
    }
    invite_accepted_id = _event_id_for(invite_accepted)
    events.append(PlannedEvent("invite_accepted", invite_accepted, invite_accepted_id, deps=(invite["invite_id"],)))

    user_data = {
        "type": "user",
        "name": args["username"],
        "invite_id": invite["invite_id"],
        "signed_by": invite["invite_id"],
        "created_at": now_ms,
    }
    user_id = _event_id_for(user_data)
    events.append(PlannedEvent("user", user_data, user_id, deps=(invite["invite_id"],)))

    peer_invite = {
        "type": "invite",
        "mode": "peer",
        "user_id": user_id,
        "signed_by": user_id,
        "created_at": now_ms,
    }
    peer_invite_id = _event_id_for(peer_invite)
    events.append(PlannedEvent("invite", peer_invite, peer_invite_id, deps=(user_id,)))

    peer_shared = {
        "type": "peer_shared",
        "user_id": user_id,
        "device_name": args["device_name"],
        "signed_by": peer_invite_id,
        "created_at": now_ms,
    }
    peer_shared_id = _event_id_for(peer_shared)
    events.append(PlannedEvent("peer_shared", peer_shared, peer_shared_id, deps=(peer_invite_id,)))

    transit_prekey = {
        "type": "transit_prekey",
        "created_at": now_ms,
    }
    transit_prekey_id = _event_id_for(transit_prekey)
    events.append(PlannedEvent("transit_prekey", transit_prekey, transit_prekey_id))

    transit_shared = {
        "type": "transit_prekey_shared",
        "prekey_id": transit_prekey_id,
        "signed_by": peer_shared_id,
        "created_at": now_ms,
    }
    transit_shared_id = _event_id_for(transit_shared)
    events.append(PlannedEvent("transit_prekey_shared", transit_shared, transit_shared_id, deps=(transit_prekey_id,)))

    if ctx.get("has_main_group_key"):
        username_update = {
            "type": "username_update",
            "user_id": user_id,
            "name": args["username"],
            "signed_by": peer_shared_id,
            "created_at": now_ms,
        }
        username_id = _event_id_for(username_update)
        events.append(PlannedEvent("username_update", username_update, username_id))
    else:
        notes.append("defer username_update until main group key arrives")

    return CreatePlan(events=tuple(events), notes=tuple(notes))


def plan_link_device_pure(args: dict[str, Any], ctx: dict[str, Any]) -> CreatePlan:
    invite = args["invite_data"]
    now_ms = ctx["now_ms"]
    events: list[PlannedEvent] = []
    notes: list[str] = []

    if invite.get("invite_prekey_id"):
        prekey_data = {
            "type": "group_prekey",
            "invite_id": invite["invite_id"],
            "created_at": now_ms,
        }
        prekey_id = _event_id_for(prekey_data)
        events.append(PlannedEvent("group_prekey", prekey_data, prekey_id))

    peer_shared = {
        "type": "peer_shared",
        "user_id": invite.get("user_id"),
        "device_name": args["device_name"],
        "signed_by": invite["invite_id"],
        "created_at": now_ms,
    }
    peer_shared_id = _event_id_for(peer_shared)
    events.append(PlannedEvent("peer_shared", peer_shared, peer_shared_id, deps=(invite["invite_id"],)))

    invite_accepted = {
        "type": "invite_accepted",
        "invite_id": invite["invite_id"],
        "invite_prekey_id": invite.get("invite_prekey_id"),
        "created_at": now_ms,
    }
    invite_accepted_id = _event_id_for(invite_accepted)
    events.append(PlannedEvent("invite_accepted", invite_accepted, invite_accepted_id, deps=(invite["invite_id"],)))

    transit_prekey = {
        "type": "transit_prekey",
        "created_at": now_ms,
    }
    transit_prekey_id = _event_id_for(transit_prekey)
    events.append(PlannedEvent("transit_prekey", transit_prekey, transit_prekey_id))

    transit_shared = {
        "type": "transit_prekey_shared",
        "prekey_id": transit_prekey_id,
        "signed_by": peer_shared_id,
        "created_at": now_ms,
    }
    transit_shared_id = _event_id_for(transit_shared)
    events.append(PlannedEvent("transit_prekey_shared", transit_shared, transit_shared_id, deps=(transit_prekey_id,)))

    if ctx.get("has_main_group_key"):
        peer_name_update = {
            "type": "peer_name_update",
            "peer_target_id": peer_shared_id,
            "name": args["device_name"],
            "signed_by": peer_shared_id,
            "created_at": now_ms,
        }
        peer_name_id = _event_id_for(peer_name_update)
        events.append(PlannedEvent("peer_name_update", peer_name_update, peer_name_id))
    else:
        notes.append("defer peer_name_update until main group key arrives")

    return CreatePlan(events=tuple(events), notes=tuple(notes))


# --------------------------------
# Imperative bootstrap sketch
# --------------------------------

def join_imperative(args: dict[str, Any], ctx: dict[str, Any], api: QueryAPI) -> CreatePlan:
    if not api.peer_exists(ctx["peer_id"]):
        raise ValueError("peer missing")

    events: list[PlannedEvent] = []
    notes: list[str] = []
    now_ms = ctx["now_ms"]

    invite = args["invite_data"]
    prekey_data = {
        "type": "group_prekey",
        "invite_id": invite["invite_id"],
        "created_at": now_ms,
    }
    prekey_id = _event_id_for(prekey_data)
    events.append(PlannedEvent("group_prekey", prekey_data, prekey_id))

    invite_accepted = {
        "type": "invite_accepted",
        "invite_id": invite["invite_id"],
        "invite_prekey_id": invite["invite_prekey_id"],
        "created_at": now_ms,
    }
    invite_accepted_id = _event_id_for(invite_accepted)
    events.append(PlannedEvent("invite_accepted", invite_accepted, invite_accepted_id, deps=(invite["invite_id"],)))

    user_data = {
        "type": "user",
        "name": args["username"],
        "invite_id": invite["invite_id"],
        "signed_by": invite["invite_id"],
        "created_at": now_ms,
    }
    user_id = _event_id_for(user_data)
    events.append(PlannedEvent("user", user_data, user_id, deps=(invite["invite_id"],)))

    peer_invite = {
        "type": "invite",
        "mode": "peer",
        "user_id": user_id,
        "signed_by": user_id,
        "created_at": now_ms,
    }
    peer_invite_id = _event_id_for(peer_invite)
    events.append(PlannedEvent("invite", peer_invite, peer_invite_id, deps=(user_id,)))

    peer_shared = {
        "type": "peer_shared",
        "user_id": user_id,
        "device_name": args["device_name"],
        "signed_by": peer_invite_id,
        "created_at": now_ms,
    }
    peer_shared_id = _event_id_for(peer_shared)
    events.append(PlannedEvent("peer_shared", peer_shared, peer_shared_id, deps=(peer_invite_id,)))

    transit_prekey = {
        "type": "transit_prekey",
        "created_at": now_ms,
    }
    transit_prekey_id = _event_id_for(transit_prekey)
    events.append(PlannedEvent("transit_prekey", transit_prekey, transit_prekey_id))

    transit_shared = {
        "type": "transit_prekey_shared",
        "prekey_id": transit_prekey_id,
        "signed_by": peer_shared_id,
        "created_at": now_ms,
    }
    transit_shared_id = _event_id_for(transit_shared)
    events.append(PlannedEvent("transit_prekey_shared", transit_shared, transit_shared_id, deps=(transit_prekey_id,)))

    if api.has_main_group_key(ctx["peer_id"]):
        username_update = {
            "type": "username_update",
            "user_id": user_id,
            "name": args["username"],
            "signed_by": peer_shared_id,
            "created_at": now_ms,
        }
        username_id = _event_id_for(username_update)
        events.append(PlannedEvent("username_update", username_update, username_id))
    else:
        notes.append("store pending username_update via pending_name_updates")

    return CreatePlan(events=tuple(events), notes=tuple(notes))


def link_device_imperative(args: dict[str, Any], ctx: dict[str, Any], api: QueryAPI) -> CreatePlan:
    if not api.peer_exists(ctx["peer_id"]):
        raise ValueError("peer missing")

    invite = args["invite_data"]
    events: list[PlannedEvent] = []
    notes: list[str] = []
    now_ms = ctx["now_ms"]

    if invite.get("invite_prekey_id"):
        prekey_data = {
            "type": "group_prekey",
            "invite_id": invite["invite_id"],
            "created_at": now_ms,
        }
        prekey_id = _event_id_for(prekey_data)
        events.append(PlannedEvent("group_prekey", prekey_data, prekey_id))

    peer_shared = {
        "type": "peer_shared",
        "user_id": invite.get("user_id"),
        "device_name": args["device_name"],
        "signed_by": invite["invite_id"],
        "created_at": now_ms,
    }
    peer_shared_id = _event_id_for(peer_shared)
    events.append(PlannedEvent("peer_shared", peer_shared, peer_shared_id, deps=(invite["invite_id"],)))

    invite_accepted = {
        "type": "invite_accepted",
        "invite_id": invite["invite_id"],
        "invite_prekey_id": invite.get("invite_prekey_id"),
        "created_at": now_ms,
    }
    invite_accepted_id = _event_id_for(invite_accepted)
    events.append(PlannedEvent("invite_accepted", invite_accepted, invite_accepted_id, deps=(invite["invite_id"],)))

    transit_prekey = {
        "type": "transit_prekey",
        "created_at": now_ms,
    }
    transit_prekey_id = _event_id_for(transit_prekey)
    events.append(PlannedEvent("transit_prekey", transit_prekey, transit_prekey_id))

    transit_shared = {
        "type": "transit_prekey_shared",
        "prekey_id": transit_prekey_id,
        "signed_by": peer_shared_id,
        "created_at": now_ms,
    }
    transit_shared_id = _event_id_for(transit_shared)
    events.append(PlannedEvent("transit_prekey_shared", transit_shared, transit_shared_id, deps=(transit_prekey_id,)))

    if api.has_main_group_key(ctx["peer_id"]):
        peer_name_update = {
            "type": "peer_name_update",
            "peer_target_id": peer_shared_id,
            "name": args["device_name"],
            "signed_by": peer_shared_id,
            "created_at": now_ms,
        }
        peer_name_id = _event_id_for(peer_name_update)
        events.append(PlannedEvent("peer_name_update", peer_name_update, peer_name_id))
    else:
        notes.append("store pending peer_name_update via pending_name_updates")

    return CreatePlan(events=tuple(events), notes=tuple(notes))


if __name__ == "__main__":
    ctx = {"seed": "demo", "now_ms": 1000, "has_main_group_key": False, "peer_id": "peer-1"}
    args = {"username": "alice", "device_name": "laptop", "network_name": "quiet"}
    plan = plan_new_network_pure(args, ctx)
    print("new_network events=", len(plan.events), "notes=", plan.notes)

    invite_data = {"invite_id": "inv-1", "invite_prekey_id": "pre-1"}
    join_args = {"username": "bob", "device_name": "phone", "invite_data": invite_data}
    plan_join = plan_join_pure(join_args, ctx)
    print("join pure events=", len(plan_join.events), "notes=", plan_join.notes)

    fake_db = FakeDB({"local_peers": {"peer-1": {"peer_id": "peer-1"}}})
    api = QueryAPI(fake_db)
    plan_imp = join_imperative(join_args, ctx, api)
    print("join imperative events=", len(plan_imp.events), "notes=", plan_imp.notes)

    link_args = {
        "device_name": "tablet",
        "invite_data": {"invite_id": "link-1", "invite_prekey_id": "pre-2", "user_id": "user-1"},
    }
    plan_link = plan_link_device_pure(link_args, ctx)
    print("link pure events=", len(plan_link.events), "notes=", plan_link.notes)
    plan_link_imp = link_device_imperative(link_args, ctx, api)
    print("link imperative events=", len(plan_link_imp.events), "notes=", plan_link_imp.notes)
