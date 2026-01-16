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
class ProjectorResult:
    writes: tuple[WriteOp, ...]
    valid_event: bool = True


@dataclass(frozen=True)
class ResolveResult:
    status: Literal["ok", "block", "reject"]
    ctx: ProjectionContext | None
    missing: tuple[str, ...] = ()
    error: str | None = None


class ResolverCache:
    """RETE-inspired cache for batch dependency resolution.

    Caches:
    - valid_events lookups (alpha memory equivalent)
    - dep row lookups (beta memory equivalent)

    Usage:
        cache = ResolverCache()
        cache.prefetch_valid_events(event_ids, recorded_by, db)
        result = resolve_event(..., cache=cache)
    """

    def __init__(self):
        # Cache: (event_id, recorded_by) -> bool (is valid)
        self._valid_events: dict[tuple[str, str], bool] = {}
        # Cache: (table, key_field, key_value, recorded_by) -> dict | None
        self._dep_rows: dict[tuple[str, str, Any, str], dict[str, Any] | None] = {}
        # Stats for debugging
        self.hits = 0
        self.misses = 0

    def is_event_valid(self, event_id: str, recorded_by: str) -> bool | None:
        """Check cache for valid event status. Returns None if not cached."""
        key = (event_id, recorded_by)
        if key in self._valid_events:
            self.hits += 1
            return self._valid_events[key]
        self.misses += 1
        return None

    def set_event_valid(self, event_id: str, recorded_by: str, is_valid: bool) -> None:
        """Cache a valid event check result."""
        self._valid_events[(event_id, recorded_by)] = is_valid

    def get_dep_row(self, table: str, key_field: str, key_value: Any, recorded_by: str) -> tuple[bool, dict[str, Any] | None]:
        """Check cache for dep row. Returns (found_in_cache, row_or_none)."""
        key = (table, key_field, key_value, recorded_by)
        if key in self._dep_rows:
            self.hits += 1
            return True, self._dep_rows[key]
        self.misses += 1
        return False, None

    def set_dep_row(self, table: str, key_field: str, key_value: Any, recorded_by: str, row: dict[str, Any] | None) -> None:
        """Cache a dep row lookup result."""
        self._dep_rows[(table, key_field, key_value, recorded_by)] = row

    def prefetch_valid_events(self, event_ids: list[str], recorded_by: str, safedb: Any) -> None:
        """Batch pre-fetch valid_events status for multiple event_ids.

        This is the key RETE optimization: single query instead of N queries.
        """
        if not event_ids:
            return

        # Filter to only IDs not already cached
        uncached = [eid for eid in event_ids if (eid, recorded_by) not in self._valid_events]
        if not uncached:
            return

        # Batch query (SQLite handles IN clauses efficiently)
        placeholders = ','.join('?' * len(uncached))
        rows = safedb.query(
            f"SELECT event_id FROM valid_events WHERE event_id IN ({placeholders}) AND recorded_by = ?",
            tuple(uncached) + (recorded_by,)
        )

        # Mark found events as valid
        valid_set = {row['event_id'] for row in rows}
        for eid in uncached:
            self._valid_events[(eid, recorded_by)] = eid in valid_set

    def stats(self) -> str:
        """Return cache statistics."""
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return f"Cache: {self.hits} hits, {self.misses} misses ({hit_rate:.1f}% hit rate)"
