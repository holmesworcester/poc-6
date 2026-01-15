# Multi-Agent Coordination

Guidelines for working with multiple Claude Code agents on this codebase.

## Worktree Strategy

Each agent should work in its own git worktree:

```bash
# Create a worktree for a feature
git worktree add /home/hwilson/poc-6-<feature> <branch-name> -b <branch-name>

# Work in isolation
cd /home/hwilson/poc-6-<feature>
# ... make changes ...

# Merge back to master
git -C /home/hwilson/poc-6 fetch /home/hwilson/poc-6-<feature> <branch>:<branch>-merge
git -C /home/hwilson/poc-6 merge <branch>-merge -m "Merge <feature>: <description>"
```

## Coordination Patterns

### Sequential Work

When one agent's work depends on another's:

1. First agent commits and pushes to their branch
2. Second agent fetches and merges before starting
3. Explicit handoff via conversation

### Parallel Work

When agents work on independent features:

1. Each agent works in their own worktree
2. Avoid touching the same files
3. Merge to master one at a time
4. Run full test suite after each merge

### Conflict Resolution

When agents modify the same files:

1. Prefer `--theirs` for generated/derived files
2. Manual merge for logic changes
3. Re-run tests after conflict resolution
4. Commit the merge with clear message

## Branch Naming

Use descriptive branch names:

```
timing-fixes          # Specific fix category
bootstrap-cleanup     # Cleanup work
file-demo             # Feature name
negentropy-sync       # Protocol feature
```

## Communication

### Handoff Format

When handing off to another agent:

```
Current state:
- Branch: <branch-name> at <worktree-path>
- Tests: N passed, M failed
- Changes: <brief summary>

Next steps:
- <what the next agent should do>

Known issues:
- <any gotchas or blockers>
```

### Status Updates

Keep the main conversation informed:

- Commit often with clear messages
- Report test results after changes
- Flag blockers immediately
- Summarize what was done before stopping

## Merge Protocol

Before merging to master:

1. **Run tests** - Full suite must pass (or known failures documented)
2. **Review changes** - `git diff --stat` to sanity check
3. **Merge cleanly** - Use descriptive merge commit message
4. **Verify** - Run tests on master after merge

```bash
# Standard merge flow
git -C /home/hwilson/poc-6 fetch <worktree> <branch>:<branch>-merge
git -C /home/hwilson/poc-6 merge <branch>-merge -m "Merge <branch>: <summary>"

# Verify
PYTHONPATH=/home/hwilson/poc-6 pytest /home/hwilson/poc-6/tests/ -v
```

## Common Issues

### Worktree Conflicts

Can't checkout a branch that's already checked out elsewhere:

```bash
# Use a different branch name for the merge reference
git fetch <source> <branch>:<branch>-merge
git merge <branch>-merge
```

### Test Environment

Always set PYTHONPATH to the worktree being tested:

```bash
PYTHONPATH=/home/hwilson/poc-6-<feature> pytest <worktree>/tests/ -v
```

### Stale Worktrees

After merging, the feature worktree may be stale. Either:
- Delete it: `git worktree remove /path/to/worktree`
- Update it: `cd <worktree> && git pull origin master`
