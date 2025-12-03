# Handshake Cleanup & Determinism Tests

## Goals

1. Fix connection handshake to match spec (unified `signed_by`)
2. Fix fragile ack matching
3. Add explicit handshake test
4. Add group_key determinism test

## Task 1: Unified `signed_by` in sync_connect

**Current (dual-signature):**
```python
connect_data = {
    'signed_by': from_peer_shared_id,    # Always peer_shared_id
    'invite_id': invite_id,               # Separate field
    # ...
}
# Plus separate invite_signature field and logic
```

**Target (spec-aligned):**
```python
connect_data = {
    'signed_by': invite_id if is_joiner else from_peer_shared_id,
    'sig': signature,  # Single signature field
    # ...
}
```

**Changes:**
- `sync_connect.send_connect()`: Set `signed_by` to invite_id OR peer_shared_id
- Remove separate `invite_id` and `invite_signature` fields
- `sync_connect.project()`: Verify `sig` using public key for `signed_by` type

## Task 2: Fix ack sender matching

**Current (fragile):**
```python
# Match by "most recent connection" - heuristic
pending_conn = unsafedb.query_one("""
    SELECT peer_shared_id FROM sync_connections
    WHERE last_seen_ms = (SELECT MAX(last_seen_ms) ...)
""")
```

**Target (robust):**
Include sender identity in ack OR use a connection token/nonce.

Option A: Include `from_peer_shared_id` in ack (simple)
```python
ack_data = {
    'type': 'sync_connect_ack',
    'from_peer_shared_id': from_peer_shared_id,  # Who is sending the ack
    'transit_key_id': ...,
    'transit_key': ...,
}
```

Option B: Use connection nonce (more secure)
- Connect includes a random nonce
- Ack echoes the nonce
- Match by nonce

**Recommendation:** Option A is simpler and sufficient. The ack is already wrapped to recipient's key, so sender identity just helps matching.

## Task 3: Explicit handshake test

Add test that verifies:
1. Bob sends sync_connect to Alice
2. Alice receives, stores Bob's transit_key, sends ack
3. Bob receives ack, stores Alice's transit_key
4. Both peers can send to each other using stored keys

```python
def test_two_way_handshake():
    # Setup Alice and Bob
    # Bob sends sync_connect to Alice
    # Verify Alice has Bob's transit_key
    # Verify Bob has Alice's transit_key (from ack)
    # Verify bidirectional: Alice can wrap to Bob, Bob can wrap to Alice
```

## Task 4: group_key determinism test

Add test parallel to `test_deterministic_prekey_ids`:

```python
def test_deterministic_group_key_ids():
    """Test: group_key events are deterministic - same key material = same key_id."""
    # Create a group_key and extract key material
    key_id1 = group_key.create(...)
    key_material = extract_from_blob(key_id1)

    # Recreate with same material
    key_id2 = group_key.create_with_material(key_material, ...)

    # Verify IDs match
    assert key_id1 == key_id2

    # Verify with different peer_id/timestamp (should still match)
    key_id3 = group_key.create_with_material(key_material, different_peer, different_time, ...)
    assert key_id1 == key_id3
```

## Files to Modify

- `events/network/sync_connect.py` - unified signed_by
- `events/network/sync_connect_ack.py` - include sender identity
- `tests/scenario_tests/test_sync_connect.py` - explicit handshake test
- `tests/scenario_tests/test_forward_secrecy.py` - group_key determinism test

## Order of Operations

1. Add tests first (they should fail or be incomplete)
2. Fix ack sender matching (simpler, fewer dependencies)
3. Unify `signed_by` (more involved, may break things)
4. Verify all tests pass
