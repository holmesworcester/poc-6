# Dict-to-SQL Migration Status

This document tracks which handlers still need to be converted from dict-based state operations to SQL.

## Summary

Based on analysis of the codebase, the following handlers still use dict-based state operations and need migration to SQL:

### Framework Tests Protocol (`protocols/framework_tests/`)
These handlers appear to have partial SQL support but still maintain dict state for backward compatibility:

1. **message handler**
   - File: `handlers/message/projector.py`
   - Status: Has SQL writes but maintains dict state for legacy tests
   - Dict patterns: `db['state']`, `state['messages']`, `state['threads']`

2. **missing_key handler**
   - Files: `handlers/missing_key/projector.py`, `handlers/missing_key/retry_pending.py`
   - Status: Has SQL writes but maintains dict state
   - Dict patterns: `db['state']`, `state['pending_missing_key']`
   - Command `retry_pending.py` still fully dict-based

3. **unknown handler**
   - Files: `handlers/unknown/projector.py`, `handlers/unknown/purge_old.py`
   - Status: Has SQL writes but maintains dict state
   - Dict patterns: `db['state']`, `state['unknown_events']`
   - Command `purge_old.py` needs SQL migration

### Message Via Tor Protocol (`protocols/message_via_tor/`)
Most handlers have been migrated to SQL, but a few still have dict fallbacks:

1. **test_helper handler**
   - File: `handlers/test_helper/load.py`
   - Status: Primarily SQL but mirrors state to dict for test readability
   - Dict patterns: `db['state'] = state` (line 136)

2. **sync_peers handler**
   - File: `handlers/sync_peers/sync_all.py`
   - Status: Needs investigation for dict usage
   - Dict patterns: `db.get('state'`

### Signed Groups Protocol (`protocols/signed_groups/`)
Several handlers still use dict state as fallbacks:

1. **invite handler**
   - Files: `handlers/invite/create.py`, `handlers/invite/projector.py`
   - Status: SQL-first but has dict fallbacks
   - Dict patterns: `db.get('state', {})` for identities/users lookup

2. **message handler**
   - File: `handlers/message/projector.py`
   - Status: SQL-first but has dict fallback for links validation
   - Dict patterns: `db.get('state', {}).get('links')`

3. **blocked handler**
   - File: `handlers/blocked/list.py`
   - Status: SQL-first but has dict fallback
   - Dict patterns: `state.get('blocked_by_id')`

4. **add handler**
   - File: `handlers/add/projector.py`
   - Status: Needs investigation

5. **group handler**
   - File: `handlers/group/projector.py`
   - Status: Needs investigation

6. **link_invite handler**
   - File: `handlers/link_invite/projector.py`
   - Status: Needs investigation

## Migration Priority

### High Priority (Pure dict operations)
1. `protocols/framework_tests/handlers/missing_key/retry_pending.py`
2. `protocols/framework_tests/handlers/unknown/purge_old.py`

### Medium Priority (Mixed dict/SQL with functional dict code)
1. `protocols/signed_groups/handlers/blocked/list.py`
2. `protocols/message_via_tor/handlers/sync_peers/sync_all.py`

### Low Priority (Dict maintained for backward compatibility only)
1. Framework test projectors that already write to SQL
2. Test helper utilities

## Notes

- Many projectors now follow a "SQL-first with dict fallback" pattern for backward compatibility
- The framework_tests protocol appears intentionally designed to test both SQL and dict modes
- Some dict state usage is only for test compatibility and may not need removal
- The `_event_store.py` modules handle SQL event storage at the protocol level