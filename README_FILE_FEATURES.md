# File Features

## 1. Download Progress Tracking

**Function**: `get_file_download_progress(file_id, recorded_by, db, prev_progress, elapsed_ms)`

**Returns**:
```python
{
    'slices_received': 186,
    'total_slices': 228,
    'bytes_received': 83500,        # Actual ciphertext bytes (SUM(LENGTH(ciphertext)))
    'percentage_complete': 81,
    'speed_bytes_per_sec': 45000,   # Calculated from actual bytes transferred
    'speed_human': '43.9 KB/s',
    'eta_seconds': 12
}
```

**Change**: Uses actual bytes from database instead of estimated `slices * avg_size`.

**Tests**: `tests/scenario_tests/test_download_progress_accuracy.py` (3 tests)

---

## 2. Data URI Upload/Download

**Upload**:
```python
# From base64
create_from_base64(peer_id, message_id, base64_data, mime_type, filename, t_ms, db, auto_compress=True)

# From data URI
create_from_data_uri(peer_id, message_id, data_uri, filename, t_ms, db, auto_compress=True)
```

**Download**:
```python
# Returns "data:image/jpeg;base64,/9j/4AAQ..."
get_file_as_data_uri(file_id, recorded_by, db, include_metadata=False)
```

**Use**: Frontend can send/receive files as base64 without HTTP endpoints.

**Tests**: `tests/test_file_data_uri.py` (9 tests)

---

## 3. Image Compression

**Function**: `compress_image_if_needed(file_data, mime_type, target_size_kb=200, max_dimension=2048)`

**Strategy**:
1. Skip if not image or already ≤200KB
2. Resize if >2048px
3. Try JPEG quality 85, 75, 65, 55, 45, 40
4. If still too large, convert to WebP

**Integration**: Automatic in `create_from_base64()` and `create_from_data_uri()` when `auto_compress=True` (default).

**Returns**:
```python
{
    'file_id': '...',
    'compressed': True,
    'original_size': 2187852,
    'final_size': 197891,
    'compression_ratio': 11.1
}
```

**Results**:
- 2.1MB JPEG → 197KB (11x)
- 3.4MB image → 25KB WebP (133x)
- Small images/non-images unchanged

**Tests**: `tests/test_image_compression.py` (7 tests)

---

## Dependencies

```bash
pip install pillow
```

## Test Status

19/19 tests passing (3 progress + 9 data URI + 7 compression)
