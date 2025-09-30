Centralizing Access Control in the Removal Handler
===================================================

**UPDATE: After further discussion, we've decided to centralize ALL validation (including access control) in the `validate` handler instead. See `validation_centralization_plan.md` for the current approach.**

Original Problem
----------------
We have multiple handlers that check different aspects of access control:
- `remove.py`: Checks if events should be removed/dropped (deletion records, type-specific removal)
- `check_membership.py`: Validates group membership for group events
- Reflectors (e.g., `sync_request_reflector`): Currently doing their own membership checks

This creates several problems:
1. Duplicated access control logic across multiple places
2. Inconsistent enforcement - some paths may bypass checks
3. Difficult to audit who has access to what
4. Hard to maintain as access rules evolve

Better Solution: Use Validate Handler
--------------------------------------
Rather than expanding the `remove` handler, we should centralize ALL validation (including access control, membership, and removal checks) in the `validate` handler. This is architecturally cleaner because:

1. **Single Responsibility**: The validate handler's job is to determine if events are valid - this naturally includes access control
2. **Clear Semantics**: Events marked `validated=true` have passed ALL checks
3. **Simpler Pipeline**: Remove handler can be eliminated or reduced to a no-op
4. **Better Name**: "Validate" better describes checking if an event should be processed

### Key Insight
Validation isn't just about data structure - it's about whether the event should be processed at all:
- Is the event structurally valid?
- Does the author have permission?
- Has anything been deleted that invalidates this event?
- Is the author a member of the required group/network?

All of these are validation concerns that should be handled in one place.

## New Implementation Plan

See `validation_centralization_plan.md` for the full implementation details. The key changes are:

1. **Validate Handler Becomes Central Validation Point**
   - All validation logic moves to `validate.py`
   - Includes structural validation, access control, membership, and removal checks
   - Single source of truth for whether an event should be processed

2. **Remove Handler Simplified or Eliminated**
   - Can become a no-op or be removed entirely
   - All removal/deletion checks move to validate handler

3. **Reflectors Simplified**
   - Remove all access/membership checks from reflectors
   - Reflectors assume validation has passed
   - Example: `sync_request_reflector` no longer checks if `to_peer` is in network

4. **Clear Pipeline Semantics**
   - `validated=true` means ALL validation passed
   - Downstream handlers trust this flag
   - No duplicate validation logic

## Benefits of This Approach

1. **Single Point of Truth**: All validation in the validate handler
2. **Consistent Enforcement**: No event can bypass validation
3. **Easier Auditing**: Can log all validation decisions in one place
4. **Simplified Components**: Reflectors and other handlers don't duplicate validation
5. **Performance**: Can short-circuit on first validation failure
6. **Flexibility**: Easy to add new validation rules in one place
7. **Clear Semantics**: `validated=true` has clear, comprehensive meaning

## Migration Steps

1. **Expand validate.py** with all validation methods (removal, access, membership)
2. **Simplify reflectors** - Remove access checks, add TODO comments
3. **Update or remove** the `remove.py` and `check_membership.py` handlers
4. **Add comprehensive tests** for validation scenarios
5. **Update documentation** to reflect centralized validation

## Current Status

- ✅ Created `sync_request/queries.py` with database queries
- ✅ Updated `sync_request/reflector.py` to use queries (no direct SQL)
- ✅ Added TODO comment in reflector about moving validation upstream
- ✅ Created comprehensive plan for validation centralization

## Next Steps

1. Implement the validation centralization as described in `validation_centralization_plan.md`
2. Remove temporary user check from `sync_request_reflector` once validation is centralized
3. Ensure all event types benefit from centralized validation

## Conclusion

By centralizing all validation (including access control) in the validate handler, we create a cleaner, more maintainable architecture. This approach:
- Eliminates duplicate validation logic
- Provides clear semantics for the `validated` flag
- Simplifies downstream components
- Makes the system easier to understand and audit