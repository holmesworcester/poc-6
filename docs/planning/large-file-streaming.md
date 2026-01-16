# Large File Streaming Design

## Problem

The current `message_attachment.create()` implementation requires the entire file to be in memory:

```python
# Current approach - holds everything in RAM:
file_data = b'X' * file_size  # 1GB allocation
slice_ciphertexts = []
for slice_number in range(0, len(file_data), SLICE_SIZE):
    plaintext_slice = file_data[slice_number:slice_number + SLICE_SIZE]
    # ... encrypt ...
    slice_ciphertexts.append(ciphertext)  # Accumulates in memory

full_ciphertext = b''.join(slice_ciphertexts)  # Another copy
root_hash = crypto.compute_root_hash(slice_ciphertexts)  # Needs all slices
```

For a 1GB file:
- `file_data`: 1GB
- `slice_ciphertexts`: ~1GB (encrypted)
- `full_ciphertext`: ~1GB (joined)
- Peak RAM: ~3GB+

## Goals

1. Support 1GB+ files without proportional RAM usage
2. Maintain backward compatibility with existing API
3. Keep the same security model (encryption, root_hash verification)

## Proposed Solution

### Option A: Streaming with Incremental Hash (Preferred)

Add a new `create_from_file()` function that streams from disk:

```python
def create_from_file(peer_id: str, message_id: str, file_path: Path,
                     filename: str | None, mime_type: str | None,
                     t_ms: int, db: Any) -> dict:
    """Create attachment from a file path, streaming to avoid memory bloat."""

    # Generate encryption key and nonce prefix
    enc_key = crypto.generate_file_encryption_key()
    nonce_prefix = crypto.generate_nonce_prefix()

    file_size = file_path.stat().st_size

    # Stream through file, encrypting slices and writing to DB immediately
    hasher = hashlib.sha256()  # Incremental root hash
    slice_count = 0

    with open(file_path, 'rb') as f:
        while True:
            plaintext_slice = f.read(SLICE_SIZE)
            if not plaintext_slice:
                break

            slice_nonce = crypto.derive_slice_nonce(nonce_prefix, slice_count * SLICE_SIZE)
            ciphertext, poly_tag = crypto.encrypt_file_slice(plaintext_slice, enc_key, slice_nonce)

            # Update hash incrementally
            hasher.update(ciphertext)

            # Write slice to DB immediately (not held in memory)
            # ... batch or individual insert ...

            slice_count += 1

    root_hash = hasher.digest()
    # ... create attachment event ...
```

**Pros:**
- Constant memory usage regardless of file size
- Simple implementation
- Clear streaming semantics

**Cons:**
- Requires changing root_hash computation (currently uses all ciphertexts at once)
- New API function (though can keep old one for small files)

### Option B: Memory-Mapped Files

Use `mmap` to let OS manage paging:

```python
import mmap

def create(peer_id, message_id, file_data_or_path, ...):
    if isinstance(file_data_or_path, (str, Path)):
        with open(file_data_or_path, 'rb') as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                return _create_impl(peer_id, message_id, mm, ...)
            finally:
                mm.close()
    else:
        return _create_impl(peer_id, message_id, file_data_or_path, ...)
```

**Pros:**
- Minimal code changes
- OS handles memory efficiently
- Works with existing slicing code

**Cons:**
- Still accumulates `slice_ciphertexts` list in memory
- `full_ciphertext = b''.join(...)` still creates full copy
- Only solves input memory, not intermediate state

### Option C: Batch Processing with Disk Temp Files

Process slices in batches, writing intermediate state to disk:

```python
def create_from_file(peer_id, message_id, file_path, ...):
    # Process in batches of 1000 slices
    BATCH_SIZE = 1000

    temp_ciphertext_file = tempfile.NamedTemporaryFile(delete=False)
    hasher = hashlib.sha256()

    with open(file_path, 'rb') as f:
        while True:
            batch = []
            for _ in range(BATCH_SIZE):
                chunk = f.read(SLICE_SIZE)
                if not chunk:
                    break
                # ... encrypt ...
                batch.append((slice_num, ciphertext, ...))

            if not batch:
                break

            # Write batch to temp file and DB
            for item in batch:
                temp_ciphertext_file.write(item.ciphertext)
                hasher.update(item.ciphertext)

            # Insert batch to DB
            file_slice.batch_create_slices(...)
            batch.clear()  # Free memory

    root_hash = hasher.digest()
```

**Pros:**
- Bounded memory usage
- Works with existing code structure

**Cons:**
- More complex
- Temp file management
- Slower due to disk I/O for temp file

## Recommended Approach

**Option A (Streaming with Incremental Hash)** is recommended:

1. Add `create_from_file()` for large files
2. Keep existing `create()` for small files (API compatibility)
3. Modify `crypto.compute_root_hash()` to support incremental updates
4. Process slices in batches for DB insertion efficiency

## Implementation Steps

1. [ ] Add `HashBuilder` class for incremental root_hash computation
2. [ ] Add `create_from_file()` function to `message_attachment.py`
3. [ ] Modify `file_slice.batch_create_slices()` to accept a generator
4. [ ] Update 1GB test to use `create_from_file()` with a temp file on disk
5. [ ] Add tests for streaming creation
6. [ ] Update docs

## Memory Budget

Target: Process 1GB file with <100MB RAM overhead

| Component | Current (1GB file) | Streaming |
|-----------|-------------------|-----------|
| Input file | 1GB (in memory) | 0 (streamed) |
| Slice ciphertexts | ~1GB (list) | ~5MB (batch) |
| Joined ciphertext | ~1GB | 0 (not needed) |
| **Total** | **~3GB** | **<100MB** |

## Security Considerations

- Root hash computation must remain deterministic
- Incremental hash must produce same result as current batch hash
- File integrity verification unchanged

## Test Plan

1. Unit test: `create_from_file()` produces same result as `create()` for small files
2. Integration test: 1GB file sync with <200MB peak RAM
3. Benchmark: Compare performance of streaming vs in-memory
