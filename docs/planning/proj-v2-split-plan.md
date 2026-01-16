# Projection v2 Parallel Split Plan (Reconstruction)

This reconstructs the multi-assistant split plan for the functional projectors
and commands worktree. It reflects current repo state and the worktrees already
created from `proj-v2-base`.

## Snapshot

- Base branch: `proj-v2-base` (scaffolding commit `Add projection v2 scaffolding`)
- Scaffolding: `core/projection_v2/*`, registry hooks in `events/registry.py`,
  and `docs/planning/projection-v2-migration.md`
- Existing worktrees created from `proj-v2-base`:
  - `/home/holmes/poc-6-proj-v2-core-codex` (branch `proj-v2-core-codex`)
  - `/home/holmes/poc-6-proj-v2-core-claude` (branch `proj-v2-core-claude`)
  - `/home/holmes/poc-6-proj-v2-recorded` (branch `proj-v2-recorded`)
- Planned additional worktrees in the original split:
  - `/home/holmes/poc-6-proj-v2-admin` (branch `proj-v2-admin`)
  - `/home/holmes/poc-6-proj-v2-pilots-codex` (branch `proj-v2-pilots-codex`)
  - `/home/holmes/poc-6-proj-v2-tests` (branch `proj-v2-tests`)

## Shared References

- `docs/planning/projection-v2-migration.md` (v2 API shape, dual-dispatch, pilot list)
- `core/projection_v2/*` (stubs: `types.py`, `resolver.py`, `engine.py`, `apply.py`)
- `events/registry.py` (hooks: `get_event_spec`, `get_project_pure_fn`)

## Pre-split Work (done before splitting)

1. Keep prototypes separate and uncommitted to avoid conflicts.
2. Add v2 scaffolding files under `core/projection_v2/`.
3. Extend `events/registry.py` to expose `EVENT_SPEC` + `project_pure`.
4. Add `docs/planning/projection-v2-migration.md`.
5. Commit scaffolding as "Add projection v2 scaffolding".
6. Create base branch `proj-v2-base`.
7. Create per-assistant worktrees off `proj-v2-base`.

## Worktree Creation Commands

```bash
git -C /home/holmes/functional-projectors-and-commands worktree add -b proj-v2-core-codex /home/holmes/poc-6-proj-v2-core-codex proj-v2-base
git -C /home/holmes/functional-projectors-and-commands worktree add -b proj-v2-core-claude /home/holmes/poc-6-proj-v2-core-claude proj-v2-base
git -C /home/holmes/functional-projectors-and-commands worktree add -b proj-v2-recorded /home/holmes/poc-6-proj-v2-recorded proj-v2-base
git -C /home/holmes/functional-projectors-and-commands worktree add -b proj-v2-admin /home/holmes/poc-6-proj-v2-admin proj-v2-base
git -C /home/holmes/functional-projectors-and-commands worktree add -b proj-v2-pilots-codex /home/holmes/poc-6-proj-v2-pilots-codex proj-v2-base
git -C /home/holmes/functional-projectors-and-commands worktree add -b proj-v2-tests /home/holmes/poc-6-proj-v2-tests proj-v2-base
```

## Workstreams and Ownership

### Workstream A: Core v2 pipeline (Codex)
Worktree: `/home/holmes/poc-6-proj-v2-core-codex`

Scope:
- Implement `resolve_event`, `apply_writes`, and `project_batch`.
- Follow the v2 API in `docs/planning/projection-v2-migration.md`.
- Do not modify `events/network/recorded.py`.

Deliverables:
- Working resolver + apply + engine.
- Minimal unit tests or targeted asserts for resolver behaviors.

### Workstream B: Core v2 pipeline (Claude)
Worktree: `/home/holmes/poc-6-proj-v2-core-claude`

Scope:
- Same as Workstream A, independently.
- This is a deliberate duplicate to compare approaches.

Deliverables:
- Alternative implementation; compare diffs with Codex version.

### Workstream C: Recorded integration (single)
Worktree: `/home/holmes/poc-6-proj-v2-recorded`

Scope:
- Update `events/network/recorded.py` for dual-dispatch:
  - If `EVENT_SPEC` + `project_pure` exist, run v2 path.
  - Otherwise use legacy `project()`.
- Reuse existing queues and blocking behavior.

Deliverables:
- Dual-dispatch in `recorded.project()` and/or `recorded.project_ids()`.
- Logging consistent with legacy path.

### Workstream D: Pilot conversions (Claude)
Worktree: `/home/holmes/poc-6-proj-v2-admin`

Scope:
- Convert pilot events to v2 (same set in migration doc).
- Recommended pair for comparison: `events/identity/network.py` and
  `events/identity/admin.py`.
- Add `EVENT_SPEC` and `project_pure`, keep legacy `project()`.

Deliverables:
- Two pilots converted with tests that compare v1 vs v2 results.

### Workstream E: Pilot conversions (Codex)
Worktree: `/home/holmes/poc-6-proj-v2-pilots-codex`

Scope:
- Convert the same pilot events as Workstream D.
- This is the second duplicate for approach comparison.

Deliverables:
- Independent conversion for side-by-side review.

### Workstream F: v2 test harness (single)
Worktree: `/home/holmes/poc-6-proj-v2-tests`

Scope:
- Add `tests/projection_v2/` with legacy vs v2 comparison tests.
- Include accept, block, and reject cases.

Deliverables:
- A small test suite usable by all workstreams.

## Integration Order

1. Merge one core implementation (A or B) into `proj-v2-base`.
2. Merge recorded integration (C).
3. Merge one pilot conversion set (D or E).
4. Merge tests (F).
5. Run full tests on the base branch.

## Short Prompts for Assistants

### Prompt 1 (Codex core)
You are in `/home/holmes/poc-6-proj-v2-core-codex` on branch `proj-v2-core-codex`.
Implement the v2 core pipeline (`core/projection_v2/resolver.py`, `apply.py`,
`engine.py`) per `docs/planning/projection-v2-migration.md`. Do not touch
`events/network/recorded.py`. Keep changes minimal and well-scoped. Report back
using the AGENTS.md handoff format.

### Prompt 2 (Claude core)
You are in `/home/holmes/poc-6-proj-v2-core-claude` on branch `proj-v2-core-claude`.
Independently implement the v2 core pipeline (resolver/apply/engine) per
`docs/planning/projection-v2-migration.md`, without touching recorded dispatch.
Keep changes minimal. Report back with handoff format.

### Prompt 3 (Recorded integration)
You are in `/home/holmes/poc-6-proj-v2-recorded` on branch `proj-v2-recorded`.
Implement dual-dispatch in `events/network/recorded.py` using
`events/registry.get_event_spec()` and `get_project_pure_fn()`. Reuse existing
queue/blocking semantics. Do not modify the v2 core stubs. Report back with
handoff format.

### Prompt 4 (Pilot conversions - Claude)
You are in `/home/holmes/poc-6-proj-v2-admin` on branch `proj-v2-admin`.
Convert `events/identity/network.py` and `events/identity/admin.py` to v2 by
adding `EVENT_SPEC` and `project_pure`, keeping legacy `project()` intact. Add
tests comparing legacy vs v2 outcomes. Report back with handoff format.

### Prompt 5 (Pilot conversions - Codex)
You are in `/home/holmes/poc-6-proj-v2-pilots-codex` on branch
`proj-v2-pilots-codex`. Convert the same two pilot events as Prompt 4 to v2.
This is for approach comparison. Add tests if needed. Report back with handoff
format.

### Prompt 6 (Tests harness)
You are in `/home/holmes/poc-6-proj-v2-tests` on branch `proj-v2-tests`.
Create `tests/projection_v2/` and implement a minimal harness that runs legacy
and v2 projection on the same inputs and compares rows. Include accept, block,
and reject cases. Report back with handoff format.
