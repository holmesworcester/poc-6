# Spec Alignment Work - Complete

This branch contains work to align the protocol specification with the implementation.

## Completed

1. **Group member authorization rule** (`docs/quiet-protocol-specification.md`)
   - Changed from "any member can add" to "only admins can add members"
   - This matches the current implementation

2. **Sign network_intro events** (`events/network/intro.py`)
   - Added signing in `create()` using `signed_by` field
   - Added signature verification in `project()`
   - Intros older than `INTRO_TTL_MS` are dropped (no pending intro writes)
     - Intros are time-sensitive for NAT hole punching
   - All 217 tests pass

## Moved to Separate Worktrees

3. **Fix encryption overhead in spec** - See `/home/hwilson/poc-6-fix-encryption-overhead`
4. **Document missing events in spec** - See `/home/hwilson/poc-6-document-missing-events`

## Related Analysis

See `/home/hwilson/.claude/plans/twinkling-roaming-hedgehog.md` for full spec vs code analysis.
