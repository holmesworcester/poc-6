"""Pure Functional Projector Framework.

Core idea:
- Projectors are pure functions: dict -> dict
- Input contains the event and all its resolved dependencies (from event source, not projections)
- Output is a dict mapping table names to rows to insert
- Framework handles dependency resolution, validation blocking, and SQL generation
"""

from typing import Any, Callable
from dataclasses import dataclass, field
import logging

log = logging.getLogger(__name__)


@dataclass
class ProjectorResult:
    """Result from a pure projector function."""
    valid: bool = True
    reason: str | None = None
    tables: dict[str, list[dict]] = field(default_factory=dict)

    # For blocking - if we're missing dependencies
    blocked: bool = False
    missing_deps: list[str] = field(default_factory=list)


@dataclass
class ProjectorSpec:
    """Specification for a projector."""
    event_type: str
    tables: list[str]  # Tables this projector can write to
    fn: Callable[[dict], ProjectorResult]


# Registry of projectors
_projectors: dict[str, ProjectorSpec] = {}


def projector(event_type: str, tables: list[str]):
    """Decorator to register a pure projector function."""
    def decorator(fn: Callable[[dict], ProjectorResult]) -> Callable[[dict], ProjectorResult]:
        spec = ProjectorSpec(event_type=event_type, tables=tables, fn=fn)
        _projectors[event_type] = spec
        return fn
    return decorator


def get_projector(event_type: str) -> ProjectorSpec | None:
    """Get projector spec for an event type."""
    return _projectors.get(event_type)


def apply_result(result: ProjectorResult, recorded_by: str, recorded_at: int, db: Any) -> bool:
    """Apply a projector result to the database.

    Returns True if applied, False if blocked or invalid.

    Note: The projector output must include all required columns explicitly.
    This function does NOT auto-add recorded_by or recorded_at - projectors
    should include these in their output if the table requires them.
    """
    if result.blocked:
        log.info(f"Projector blocked, missing deps: {result.missing_deps}")
        return False

    if not result.valid:
        log.warning(f"Projector validation failed: {result.reason}")
        return False

    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)

    for table_name, rows in result.tables.items():
        for row in rows:
            # Build INSERT OR IGNORE statement
            columns = list(row.keys())
            placeholders = ', '.join(['?' for _ in columns])
            column_list = ', '.join(columns)
            values = [row[c] for c in columns]

            sql = f"INSERT OR IGNORE INTO {table_name} ({column_list}) VALUES ({placeholders})"
            safedb.execute(sql, tuple(values))

    return True


def validate_output(result: ProjectorResult, spec: ProjectorSpec) -> None:
    """Validate that projector output only touches declared tables."""
    for table_name in result.tables.keys():
        if table_name not in spec.tables:
            raise ValueError(f"Projector for {spec.event_type} wrote to undeclared table: {table_name}. Declared: {spec.tables}")
