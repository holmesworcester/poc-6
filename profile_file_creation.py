#!/usr/bin/env python3
"""Profile file creation steps to find the bottleneck."""

import sqlite3
import time
from db import Database
import schema
from events.identity import user, invite, peer
from events.content import message, message_attachment
import logging
import crypto

logging.getLogger().setLevel(logging.CRITICAL)

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

alice = user.new_network(name='Alice', t_ms=1000, db=db)
msg_result = message.create(peer_id=alice['peer_id'], channel_id=alice['channel_id'],
                           content='Test', t_ms=4000, db=db)
message_id = msg_result['id']

# Create 50 MB file data
file_data = b'X' * (50 * 1024 * 1024)
print(f"File size: {len(file_data):,} bytes")

# Profile the slicing process
print("\n=== Profiling file slicing ===")

SLICE_SIZE = 450
start = time.perf_counter()
slice_ciphertexts = []
slices_to_create = []

for slice_number in range(0, len(file_data), SLICE_SIZE):
    plaintext_slice = file_data[slice_number:slice_number + SLICE_SIZE]
    nonce_prefix = crypto.generate_secret()[:20]  # This should be outside, but profiling as-is
    slice_nonce = crypto.derive_slice_nonce(nonce_prefix, slice_number)
    ciphertext, poly_tag = crypto.encrypt_file_slice(plaintext_slice, crypto.generate_secret(), slice_nonce)
    slice_ciphertexts.append(ciphertext)
    slices_to_create.append((slice_number, slice_nonce, ciphertext, poly_tag))

slice_time = time.perf_counter() - start
print(f"Slicing + encryption: {slice_time:.3f}s for {len(slices_to_create)} slices")

# Profile file_id and root_hash computation
start = time.perf_counter()
full_ciphertext = b''.join(slice_ciphertexts)
file_id = crypto.compute_file_id(full_ciphertext)
root_hash = crypto.compute_root_hash(slice_ciphertexts)
hash_time = time.perf_counter() - start
print(f"File ID + root hash computation: {hash_time:.3f}s")

# Now measure just the slice creation loop
print("\n=== Profiling slice event creation ===")

# Get peer_shared_id
from db import create_safe_db
safedb = create_safe_db(db, recorded_by=alice['peer_id'])
peer_self_row = safedb.query_one(
    "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
    (alice['peer_id'], alice['peer_id'])
)
peer_shared_id = peer_self_row['peer_shared_id']

start = time.perf_counter()
from events.content import file_slice
slice_count = file_slice.batch_create_slices(
    file_id=file_id,
    slices_data=slices_to_create,
    peer_id=alice['peer_id'],
    created_by=peer_shared_id,
    t_ms=5000,
    db=db
)
creation_time = time.perf_counter() - start
print(f"Batch create {slice_count} slices: {creation_time:.3f}s")

# Profile creating message_attachment event
start = time.perf_counter()

from events.content import message_attachment as ma_module
from events.transit import recorded

# Get group_id
message_row = safedb.query_one(
    "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
    (message_id, alice['peer_id'])
)
group_id = message_row['group_id']

attachment_event_data = {
    'type': 'message_attachment',
    'message_id': message_id,
    'file_id': file_id,
    'filename': 'test.dat',
    'mime_type': 'application/octet-stream',
    'blob_bytes': len(file_data),
    'enc_key': crypto.b64encode(crypto.generate_secret()),
    'nonce_prefix': crypto.b64encode(crypto.generate_secret()[:20]),
    'root_hash': crypto.b64encode(root_hash),
    'total_slices': slice_count,
    'created_by': peer_shared_id,
    'created_at': 5000
}

canonical = crypto.canonicalize_json(attachment_event_data)
from events.content.message_attachment import group_key_encrypt
import store
wrapped = group_key_encrypt(canonical, alice['peer_id'], group_id, db)
event_id = store.event(wrapped, alice['peer_id'], 5000, db)

attachment_time = time.perf_counter() - start
print(f"Create message_attachment event: {attachment_time:.3f}s")

db.commit()

total = slice_time + hash_time + creation_time + attachment_time
print(f"\nTotal: {total:.3f}s")
print(f"  Slicing + encryption: {slice_time/total*100:.1f}%")
print(f"  Hash computation: {hash_time/total*100:.1f}%")
print(f"  Slice event creation: {creation_time/total*100:.1f}%")
print(f"  Attachment event: {attachment_time/total*100:.1f}%")
