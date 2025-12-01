"""Pure Functional Projectors.

Each projector defines:
  SPEC - declares encrypted, signer_type, dependencies, tables
  project(input_dict) - pure function: dict -> ProjectorResult

Pure functional creation:
  DEPS - declares dependencies needed for creation
  create_pure(deps, ...) - pure function: deps -> CreateResult

The framework (in project.py) handles:
  resolve() - generic resolution driven by SPEC
  apply_result() - writes to database with INSERT OR IGNORE
  dispatch() - event_type -> projector
  check_deps() - semantic dependency checking
  cleanup_deleted_events() - cascade deletion at end of transaction
  resolve_create_deps() - gather dependencies for creation
  store_create_result() - store all blobs from a create result
"""

from projectors.project import (
    # Projection types and functions
    ProjectorResult,
    resolve,
    apply_result,
    apply_result_device_wide,
    project_event,
    dispatch,
    check_deps,
    is_foreign_local_dep,
    get_spec,
    cleanup_deleted_events,
    DATA_TABLES,
    # Pure creation types and functions
    BlobSpec,
    CreateResult,
    compute_event_id,
    resolve_create_deps,
    store_create_result,
)

__all__ = [
    # Projection
    'ProjectorResult',
    'resolve',
    'apply_result',
    'apply_result_device_wide',
    'project_event',
    'dispatch',
    'check_deps',
    'is_foreign_local_dep',
    'get_spec',
    'cleanup_deleted_events',
    'DATA_TABLES',
    # Pure creation
    'BlobSpec',
    'CreateResult',
    'compute_event_id',
    'resolve_create_deps',
    'store_create_result',
]
