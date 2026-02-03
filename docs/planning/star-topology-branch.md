# Star Topology Branch - TreeKEM + O(1) Key Distribution

## Summary

This branch implements TreeKEM-based O(1) key distribution with sender keys, replacing the previous O(n) per-message encryption. The system achieves:

- **O(1) broadcast**: Single encrypted event reaches all group members via root secret
- **O(log n) updates**: TreeKEM copath encryption for key updates
- **O(log n) removals**: Tree cover with old pubkeys, no re-encryption cascade
- **Forward secrecy**: Removal epochs isolate key material from removed users

## Commits (oldest to newest)

1. **Add TreeKEM Phase 1 and Phase 2 event types** - Core event types for O(log n) key distribution
2. **Fix TreeKEM Phase 1/2 APIs and add online/offline simulation** - API refinements
3. **Wire removal_epoch into key distribution** - Forward secrecy via epoch isolation
4. **Add transitive dependencies for atomic TreeKEM update paths** - Atomic multi-event projection
5. **Complete sender key integration with replay-safe architecture** - Sender keys with nonce tracking
6. **Add O(1) root broadcast via path secret KDF derivation** - Root secret convergence
7. **Add connection limiting (k=20) for scale tests** - Make O(n^2) mesh tractable
8. **Add DH-based TreeKEM for O(1) root convergence (BeeKEM style)** - DH key agreement for root
9. **Add scale tests for messaging and user removal** - 100-1000 user scale tests
10. **Replace ad-hoc 100-user test with proper scenario test** - Test cleanup
11. **Add star topology fanout for near-instant event delivery** - Server-assisted broadcast
12. **Merge treekem-sender-key branch** - Consolidation

## Key Components

### Event Types (in `events/group/`)

| Event | Purpose |
|-------|---------|
| `secret.py` | Sender key secrets (symmetric keys for message encryption) |
| `secret_shared.py` | Encrypted secret distribution to recipients |
| `secret_broadcast.py` | O(1) root-encrypted broadcast of secrets |
| `treekem_pubkey.py` | Local TreeKEM public keys at tree positions |
| `treekem_pubkey_shared.py` | Shared TreeKEM pubkeys for copath encryption |
| `treekem_secret.py` | TreeKEM path secrets at tree positions |
| `treekem_secret_shared.py` | Encrypted path secrets for copath members |
| `treekem_update.py` | TreeKEM update events with DH key agreement |
| `treekem_reupdate.py` | Re-update mechanism for concurrent update convergence |
| `key_request.py` | Key request/fulfillment for partition healing |
| `sender_key.py` | Key distribution orchestration (O(1)/O(log n)/O(n) paths) |

### Core Modules

| Module | Purpose |
|--------|---------|
| `core/treekem.py` | TreeKEM tree utilities (copath, path prefix, coverage) |
| `core/jobs.py` | `TreeKEMUpdateJob`, `TreeKEMReUpdateJob`, `KeyRequestJob` |

### Key Distribution Paths

```
distribute_key_to_group()
    │
    ├─► Has root secret? ──► O(1) broadcast (secret_broadcast)
    │
    ├─► Has copath pubkeys? ──► O(log n) tree cover
    │
    └─► Fallback ──► O(n) leaf-by-leaf encryption
```

## Concurrent Update Handling

When multiple peers post updates with the same `base_update_id`:
1. **Deterministic winner**: Lexicographically lowest update ID wins
2. **Re-update mechanism**: `TreeKEMReUpdateJob` detects superseded updates
3. **Convergence**: All peers eventually derive same root secret

## Test Coverage

### Scale Tests (`tests/scenario_tests/test_treekem_scale.py`)

- `test_scale_o1_broadcast_after_all_updates[100]` - O(1) with 100 members
- `test_scale_leaf_fallback_before_updates[100]` - O(n) fallback before TreeKEM
- `test_scale_removal_uses_old_pubkeys[100]` - Removal doesn't break distribution
- `test_scale_o1_broadcast_1k` (slow) - 1,000 member scale test
- `test_scale_o1_broadcast_10k` (slow) - 10,000 member stress test

### Unit Tests

- `test_treekem_phase1.py` - Phase 1 events (removal_epoch, key_request, etc.)
- `test_treekem_phase2.py` - Phase 2 events (treekem_update, copath, etc.)
- `test_dh_treekem.py` - DH-based key agreement
- `test_dh_root_convergence.py` - Root secret convergence

## Uncommitted Work

- `test_treekem_scale.py`: TestClock fix for timestamp consistency
- `key_request.py`: Additional key request handling
- `core/jobs.py`: Job scheduling updates

## Related Branches (Superseded)

- `treekem-sender-key` - Fully contained in this branch
- `multicast-treekem-test` - Fully contained in this branch
- `treekem-ologn` - Contains planning doc + separate sync optimizations (NOT merged)

## Next Steps

1. Merge to master after review
2. Update protocol specification with new event types
3. Consider merging sync optimizations from `treekem-ologn` separately
