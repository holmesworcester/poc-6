# Claude Code Guidelines

Guidance for Claude Code when working on this codebase.

## Before Making Changes

1. **Read RULES.md** - Core principles and gotchas
2. **Read the spec** - `docs/quiet-protocol-specification.md` is authoritative
3. **Check existing patterns** - Search for similar code before inventing new patterns
4. **Run tests first** - Know the baseline before changing anything

## Event System

This is an event-sourced system. Every state change flows through events:

```
create() → store.event() → recorded.project() → type.project() → table
```

### Creating Events

```python
# Standard pattern - use the event module's create function
event_id = some_event.create(
    field1=value1,
    peer_id=peer_id,
    t_ms=t_ms,
    db=db
)
```

The `create()` function handles signing and calls `store.event()` which handles projection.

### Common Mistakes to Avoid

1. **Don't add timestamp offsets** - Use `t_ms=t_ms`, not `t_ms=t_ms+10`
2. **Don't force projection** - `store.event()` already projects
3. **Don't bypass checks** - Fix dependency ordering instead
4. **Don't insert directly into tables** - Go through events

## Testing Changes

```bash
# Run specific test
PYTHONPATH=. pytest tests/scenario_tests/test_one_player_messaging.py -v

# Run all tests
PYTHONPATH=. pytest tests/ -v

# Quick check
PYTHONPATH=. pytest tests/scenario_tests/ -v --tb=short
```

Always verify tests pass before committing.

## Key Files to Understand

| File | Purpose |
|------|---------|
| `store.py` | Event storage and projection trigger |
| `events/network/recorded.py` | Dependency checking and projection dispatch |
| `events/identity/user.py` | Bootstrap flow (`new_network()`, `join()`) |
| `events/identity/invite.py` | Invite creation and admin checks |
| `schema.py` | Database schema definitions |

## Debugging Tips

### Event Not Projecting?

1. Check `check_deps()` - what deps are missing?
2. Verify the dependency event is in `valid_events`
3. Check if it's blocked in `blocked_events_ephemeral`

### Authorization Failing?

1. Is `admin_grant` projected before the admin-gated operation?
2. Is `is_admin()` finding the user in `admins` table?
3. Is `peers_shared` correctly mapping peer to user?

### Sync Not Working?

1. Is the event in `shareable_events`?
2. Is it marked with correct `recorded_by`?
3. Check `sync.py` logs for the sync round

## Commit Guidelines

- Run tests before committing
- One logical change per commit
- Clear commit messages explaining "why"
- Include `Co-Authored-By: Claude <noreply@anthropic.com>` footer

## When in Doubt

1. Check how existing code handles the same pattern
2. Read the relevant section of `quiet-protocol-specification.md`
3. Look at test scenarios for expected behavior
4. Ask before adding "defensive" code or bypass flags
