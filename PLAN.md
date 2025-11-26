# Plan: Eliminate Blob Unpacking Heuristics

## Problem

Current implementation (message_attachment.py:714-770) uses fragile heuristics to unpack consolidated file slices:

```python
expected_ciphertext_len = SLICE_SIZE  # Assumes ciphertext == plaintext size
poly_tag = consolidated_blob[offset+expected_ciphertext_len:offset+expected_ciphertext_len+16]
ciphertext = consolidated_blob[offset:offset+expected_ciphertext_len]
```

**Issues**:
- Comment says "heuristic" = brittle code
- Ciphertext size may vary slightly from plaintext (AEAD overhead)
- No fallback if heuristic fails mid-stream
- Hard to debug when unpacking fails

## Solution

Store slice boundaries deterministically in database instead of computing from heuristics.

## Implementation Steps

### Phase 1: Extend file_slices schema

1. **Read current schema**: `schema.py` - find `file_slices` table definition
2. **Add slice boundary columns**:
   ```sql
   ALTER TABLE file_slices ADD COLUMN (
       blob_offset_start INTEGER,  -- Byte offset in consolidated blob
       blob_offset_end INTEGER,    -- Byte offset in consolidated blob
       ciphertext_len INTEGER      -- Exact ciphertext length
   );
   ```
3. **Rationale**: Store what we know (actual sizes) instead of computing from heuristics

### Phase 2: Update slice creation

1. **File**: `events/content/file_slice.py` - `batch_create_slices()` function
2. **Changes**:
   - Track cumulative blob offset as slices are created
   - Store `blob_offset_start`, `blob_offset_end`, `ciphertext_len` for each slice
   - Calculate boundaries before consolidation happens

### Phase 3: Update consolidation

1. **File**: `message_attachment.py` - `consolidate_file_slices()` function
2. **Current behavior**: Reads from `file_slices`, concatenates slices into blob
3. **New behavior**: SAME (consolidation is correct)
4. **Only change**: Optionally record which byte ranges went where (for verification)

### Phase 4: Fix unpacking

1. **File**: `message_attachment.py` - `get_file_data()` function (lines 714-770)
2. **Replace heuristic unpacking**:
   ```python
   # OLD (heuristic)
   expected_ciphertext_len = SLICE_SIZE
   poly_tag = consolidated_blob[offset+expected_ciphertext_len:...]

   # NEW (deterministic)
   slice_row = slice_rows[slice_num]
   offset = slice_row['blob_offset_start']
   ciphertext_len = slice_row['ciphertext_len']
   nonce = consolidated_blob[offset:offset+12]
   ciphertext = consolidated_blob[offset+12:offset+12+ciphertext_len]
   poly_tag = consolidated_blob[offset+12+ciphertext_len:offset+12+ciphertext_len+16]
   ```
3. **Benefits**:
   - No heuristics - exact boundaries from DB
   - Handles variable ciphertext sizes correctly
   - Clear, self-documenting code

### Phase 5: Add tests

1. **File**: `tests/scenario_tests/test_file_data_uri.py` (already exists)
2. **Add test case**: "Test unpacking with variable ciphertext sizes"
   - Create file with ciphertext sizes that vary from SLICE_SIZE
   - Verify unpacking works correctly
   - This would have caught the original heuristic bug

## Files to Modify

| File | Change | Complexity |
|------|--------|-----------|
| schema.py | Add 3 columns to file_slices | Low |
| events/content/file_slice.py | Track offsets during creation | Medium |
| message_attachment.py | Replace heuristic unpacking | Medium |
| tests/scenario_tests/test_file_data_uri.py | Add variable-size test | Low |

## Success Criteria

- [ ] `get_file_data()` never uses heuristics - all boundaries from DB
- [ ] `consolidated_blob` unpacking works with non-standard ciphertext sizes
- [ ] All existing tests pass
- [ ] New test verifies variable ciphertext handling
- [ ] Code has zero comments about "heuristics"

## Testing Strategy

1. **Unit test**: Direct unpacking with known boundaries
2. **Integration test**: File creation → consolidation → unpacking round-trip
3. **Edge case test**: Very small files (single slice, partial slice)
4. **Property test**: Random ciphertext sizes, verify unpacking always works

## Risk Assessment

**Risk Level**: Low
- Only affects file unpacking path
- Falls back to slow path on error (existing code)
- Fully backward compatible (adds columns, doesn't remove)
- All tests should pass before merge

## Estimated Effort

- Analysis: 1 hour
- Implementation: 2-3 hours
- Testing: 1-2 hours
- Total: 4-6 hours

## Benefits

✓ Eliminates fragile heuristic logic
✓ Makes code self-documenting (boundaries in DB)
✓ Enables proper error handling
✓ Fixes potential bugs with variable-size ciphertexts
