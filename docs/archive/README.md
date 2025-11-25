# Documentation Archive

This directory contains documentation that is no longer actively relevant but preserved for historical reference.

## Archive Categories

### Completed Implementations
Documentation for features that have been fully implemented and integrated into the system.

### Superseded Designs
Design documents that were replaced by later approaches or implementations.

### Reference Notes
Temporary notes and debugging docs from development work.

---

## Archived Documents

### Completed Implementations
- **disappearing_messages_implementation_plan.md** - Archived 2025-11-25: Implementation complete, superseded by disappearing_messages.md summary

### Duplicate/Superseded Content
- **summary.md** - Archived 2025-11-25: Duplicate content of focused_file_sync.md
- **self_connection_bug.md** - Archived 2025-11-25: Bug fixed in commit e1cfb6b

### Reference Notes
- **bash_env_notes.md** - Archived 2025-11-25: Bash environment workaround notes, may be obsolete
- **recorded_py_changes.md** - Archived 2025-11-25: Historical record of code changes

---

## Archiving Process

When archiving a document:

1. **Verify obsolescence** - Confirm the document is truly no longer needed
2. **Move the file** - `git mv docs/FILENAME.md docs/archive/`
3. **Update this README** - Add entry under appropriate category with date and reason
4. **Update active docs** - Remove references from docs/README.md if linked
5. **Commit** - Document why in the commit message

## Searching Archives

Use git to search archived docs:
```bash
grep -r "search term" docs/archive/
git log -p docs/archive/ -- FILENAME.md  # View history
```

## Restoration

To restore an archived doc:
```bash
git mv docs/archive/FILENAME.md docs/FILENAME.md
# Update docs/README.md if appropriate
```

All content remains in git history, nothing is lost.
