"""Pure Functional Projectors.

Each projector defines:
  SPEC - declares encrypted, signer_type, dependencies, tables
  project(input_dict) - pure function: dict -> ProjectorResult

The framework (in project.py) handles:
  resolve() - generic resolution driven by SPEC
  apply_result() - writes to database with INSERT OR IGNORE
  dispatch() - event_type -> projector
  check_deps() - semantic dependency checking
"""

from projectors.project import (
    ProjectorResult,
    resolve,
    apply_result,
    project_event,
    dispatch,
    check_deps,
    is_foreign_local_dep,
    get_spec,
)

__all__ = [
    'ProjectorResult',
    'resolve',
    'apply_result',
    'project_event',
    'dispatch',
    'check_deps',
    'is_foreign_local_dep',
    'get_spec',
]
