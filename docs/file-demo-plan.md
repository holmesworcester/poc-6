# File Demo CLI Feature Plan

## Goal
Demo file uploading/downloading in the CLI with progress indicators.

## UI Changes

### 1. Message Display (Hybrid - inline progress)
Show attachment info with percentage on each message:

```
MAIN (#general):
  1. [1000ms] alice: Check out this photo!
     [img 200KB 100%]
  2. [2000ms] bob: Sending you a big file
     [bin 1.0GB 42%]
  3. [3000ms] charlie: Nice weather today
```

Format: `[type size progress%]`
- type: `img` for image/*, `bin` for other
- size: human readable (200KB, 1.0GB)
- progress: percentage for receiver, always 100% for sender

### 2. `files` Command - Detailed Progress View
```
> files
FILES:
  In Progress:
    1. ↓ data.bin      [████░░░░░░] 42% | 5.2 MB/s | ETA 2m
    2. ↓ video.mp4     [██░░░░░░░░] 18% | 3.1 MB/s | ETA 8m

  Complete:
    3. ✓ photo.png     200 KB
    4. ✓ doc.pdf       1.2 MB
```

## New Commands

### `send-with-image <message>`
- Creates message with text content
- Attaches 200KB of random data
- filename: `demo-image-{timestamp}.bin`
- mime_type: `image/png` (to demo image handling)

### `send-with-gb`
- Creates message with generic text
- Attaches 1GB of random data
- filename: `demo-large-{timestamp}.bin`
- mime_type: `application/octet-stream`

### `files`
- Lists all file attachments visible to current account
- Shows in-progress downloads with progress bar, speed, ETA
- Shows completed files with size

### `pause <n>` / `resume <n>`
- Pause/resume file download by number from `files` list

## Implementation Steps

1. **Modify `display_main()`** in cli.py
   - After displaying message content, check for attachments
   - For each attachment, get progress via `get_file_download_progress()`
   - Display `[type size progress%]` on next line

2. **Add `cmd_send_with_image()`**
   - Generate 200KB random data: `os.urandom(200 * 1024)`
   - Create message, then attach file

3. **Add `cmd_send_with_gb()`**
   - Generate 1GB random data (streaming to avoid memory issues)
   - Create message, then attach file

4. **Add `cmd_files()`**
   - Query all attachments for current account
   - Group by in-progress vs complete
   - Display with progress bars

5. **Add `cmd_pause()` / `cmd_resume()`**
   - Map file number to file_id
   - Call `sync_file.pause_file_sync()` / `resume_file_sync()`

6. **Update help text**

## File Changes
- `cli.py` - all changes in single file
