# Spec Alignment Work In Progress

This branch contains work to align the protocol specification with the implementation.

## Completed

1. **Group member authorization rule** (`docs/quiet-protocol-specification.md`)
   - Changed from "any member can add" to "only admins can add members"
   - This matches the current implementation

## In Progress

2. **Sign network_intro events** (`events/network/intro.py`)
   - Added signing in `create()` using `signed_by` field
   - Added signature verification in `project()`
   - **FAILING TEST**: `test_nat_hole_punch_simple`
   - **Issue**: When Bob projects Alice's intro, he doesn't have Alice's peer_shared yet
   - **Fix needed**: Either:
     - Ensure dependency checking blocks intro until signed_by is valid, OR
     - Make `get_public_key()` return None instead of raising, allowing graceful blocking

## Still To Do

3. **Fix encryption overhead in spec** - Spec says 40 bytes, actual is 56/88 bytes
4. **Document missing events in spec** - message_attachment, reactions, etc.

## Related Analysis

See `/home/hwilson/.claude/plans/twinkling-roaming-hedgehog.md` for full spec vs code analysis.
