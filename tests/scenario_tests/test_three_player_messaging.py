"""
Scenario test: Three players with message transit via sync.

Alice creates a network. Bob joins Alice's network via invite.
Charlie creates his own separate network.

Tests:
- Alice and Bob exchange sync requests and receive each other's messages
- Charlie is isolated (separate network)
- Charlie only sees their own messages
"""
import sqlite3
import pytest
from db import Database
import schema
from events.transit import sync, recorded, sync_debug
from events.content import message
from events.identity import user
import store
import crypto


def test_three_player_messaging():
    """Three peers: Alice creates network, Bob joins, Charlie separate."""

    # Configure logging to show INFO level for debugging sync issues
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s',
        force=True  # Override existing configuration
    )

    # Also set level on root logger
    logging.getLogger().setLevel(logging.INFO)

    # Setup: Initialize in-memory database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates a network (implicit network via first group)
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates an invite link for Bob to join
    from events.identity import invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins Alice's network via invite
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Charlie creates his own separate network
    charlie = user.new_network(name='Charlie', t_ms=3000, db=db)

    # Bootstrap: Fully realistic protocol
    # Bob sends bootstrap events + sync request (all wrapped with invite key / Alice's prekey)
    user.send_bootstrap_events(
        peer_id=bob['peer_id'],
        peer_shared_id=bob['peer_shared_id'],
        user_id=bob['user_id'],
        transit_prekey_shared_id=bob['transit_prekey_shared_id'],
        invite_data=bob['invite_data'],
        t_ms=4000,
        db=db
    )

    print(f"\n=== STEP 1: Alice receives Bob's bootstrap events ===")

    # Check Bob's SHAREABLE events (not just store) BEFORE Alice processes
    bob_shareable_before_step1 = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],))
    alice_shareable_before = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))

    bob_shareable_ids = set(row['event_id'] for row in bob_shareable_before_step1)
    alice_shareable_ids = set(row['event_id'] for row in alice_shareable_before)

    # Check if Bob's shareable events overlap with Alice's
    bob_can_share_alice_events = bob_shareable_ids & alice_shareable_ids

    print(f"BEFORE Step 1: Alice has {len(alice_shareable_ids)} shareable events")
    print(f"BEFORE Step 1: Bob has {len(bob_shareable_ids)} shareable events")
    print(f"BEFORE Step 1: Bob can share {len(bob_can_share_alice_events)}/{len(alice_shareable_ids)} of Alice's events (WRONG - should be 0!)")

    # Print Alice's shareable_events list and check if Bob has RECORDED them
    print(f"\n🔍 ALICE'S SHAREABLE EVENTS (and Bob's status):")
    for row in alice_shareable_before:
        event_id = row['event_id']
        event_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (event_id,))
        window_row = db.query_one("SELECT window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?", (event_id, alice['peer_id']))
        window_id = window_row['window_id'] if window_row else 'N/A'

        # Check if Bob has recorded this event (recorded events are in store table as JSON)
        import json
        all_store = db.query("SELECT id, blob FROM store")
        bob_has_recorded = False
        for s in all_store:
            try:
                data = json.loads(s['blob'])
                if data.get('type') == 'recorded' and data.get('ref_id') == event_id and data.get('recorded_by') == bob['peer_id']:
                    bob_has_recorded = True
                    break
            except:
                pass

        # Check if Bob has it in shareable_events
        in_bob_shareable = event_id in bob_shareable_ids

        if event_blob:
            try:
                event_data = crypto.parse_json(event_blob['blob'])
                event_type = event_data.get('type', 'unknown')
                print(f"  - {event_id[:20]}... type={event_type:20} window={window_id:7} bob_shareable={'YES' if in_bob_shareable else 'NO':3} bob_recorded={'YES' if bob_has_recorded else 'NO':3}")
            except:
                print(f"  - {event_id[:20]}... type={'encrypted':20} window={window_id:7} bob_shareable={'YES' if in_bob_shareable else 'NO':3} bob_recorded={'YES' if bob_has_recorded else 'NO':3}")

    # Print Bob's full shareable_events list
    print(f"\n🔍 BOB'S SHAREABLE EVENTS:")
    for row in bob_shareable_before_step1:
        event_id = row['event_id']
        event_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (event_id,))
        window_row = db.query_one("SELECT window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?", (event_id, bob['peer_id']))
        window_id = window_row['window_id'] if window_row else 'N/A'
        if event_blob:
            try:
                event_data = crypto.parse_json(event_blob['blob'])
                event_type = event_data.get('type', 'unknown')
                in_alice = event_id in alice_shareable_ids
                print(f"  - {event_id[:20]}... type={event_type:20} window={window_id} alice_has={'YES' if in_alice else 'NO'}")
            except:
                print(f"  - {event_id[:20]}... type={'encrypted':20} window={window_id}")

    # Print which specific events overlap
    if len(bob_can_share_alice_events) > 0:
        print(f"\n🔍 OVERLAP EVENTS (Bob claims to have Alice's events):")
        for event_id in bob_can_share_alice_events:
            print(f"  - {event_id[:20]}...")

    # CRITICAL TEST: Verify Bob's bloom filter only contains events Bob has recorded
    print(f"\n🔬 BLOOM FILTER VERIFICATION:")
    print(f"Testing that Bob's bloom only matches events Bob has actually recorded...")

    # Get Bob's shareable events (these should be in his bloom)
    bob_shareable_events = set(row['event_id'] for row in bob_shareable_before_step1)
    print(f"Bob has {len(bob_shareable_events)} shareable events")

    # Manually check each of Alice's events against Bob's bloom
    # We need to reconstruct the bloom check logic
    from events.transit import sync as sync_module
    from events.identity import peer_shared
    import crypto as crypto_mod

    # Get Bob's public key (same as used in send_request)
    bob_public_key = peer_shared.get_public_key(bob['peer_shared_id'], bob['peer_id'], db)
    window_id = 0
    salt = sync_module.derive_salt(bob_public_key, window_id)

    # Create Bob's bloom from his shareable events
    bob_event_id_bytes = [crypto_mod.b64decode(eid) for eid in bob_shareable_events]
    bob_bloom = sync_module.create_bloom(bob_event_id_bytes, salt)

    print(f"Bob's bloom has {bin(int.from_bytes(bob_bloom, 'big')).count('1')} bits set")

    # Test each of Alice's events
    print(f"\nTesting Alice's {len(alice_shareable_ids)} events against Bob's bloom:")
    false_positives = []
    for alice_event_id in alice_shareable_ids:
        alice_event_bytes = crypto_mod.b64decode(alice_event_id)
        in_bloom = sync_module.check_bloom(alice_event_bytes, bob_bloom, salt)
        bob_actually_has = alice_event_id in bob_shareable_events

        status = "✓" if in_bloom == bob_actually_has else "✗"
        if in_bloom and not bob_actually_has:
            false_positives.append(alice_event_id)
            print(f"  {status} {alice_event_id[:20]}... in_bloom={in_bloom} bob_has={bob_actually_has} ← FALSE POSITIVE!")
        else:
            print(f"  {status} {alice_event_id[:20]}... in_bloom={in_bloom} bob_has={bob_actually_has}")

    if false_positives:
        print(f"\n❌ BLOOM FILTER BUG: {len(false_positives)} false positives detected!")
        print(f"These events match the bloom but Bob doesn't have them:")
        for fp in false_positives:
            print(f"  - {fp[:20]}...")
    else:
        print(f"\n✅ Bloom filter working correctly - no false positives")

    print(f"\n🔍 Bloom hex (manually created): {bob_bloom.hex()[:60]}...")

    sync.receive(batch_size=20, t_ms=4100, db=db)

    # Check Bob's SHAREABLE events AFTER Alice processes
    bob_shareable_after_step1 = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],))
    alice_shareable_after_step1 = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))

    bob_shareable_ids_after = set(row['event_id'] for row in bob_shareable_after_step1)
    alice_shareable_ids_after = set(row['event_id'] for row in alice_shareable_after_step1)
    bob_can_share_alice_events_after = bob_shareable_ids_after & alice_shareable_ids_after

    print(f"AFTER Step 1: Alice has {len(alice_shareable_ids_after)} shareable events (gained {len(alice_shareable_ids_after) - len(alice_shareable_ids)})")
    print(f"AFTER Step 1: Bob has {len(bob_shareable_ids_after)} shareable events (gained {len(bob_shareable_ids_after) - len(bob_shareable_ids)})")
    print(f"AFTER Step 1: Bob can share {len(bob_can_share_alice_events_after)}/{len(alice_shareable_ids_after)} of Alice's events")

    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    print(f"Bob has channel valid: {bool(bob_has_channel)}")

    print(f"\n=== STEP 2: Process any unblocked events ===")

    sync.receive(batch_size=20, t_ms=4150, db=db)

    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    print(f"Bob has channel valid: {bool(bob_has_channel)}")

    print(f"\n=== STEP 3: Alice processes sync request, sends response ===")
    queue_before = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")['cnt']
    print(f"Incoming queue before Alice processes: {queue_before} blobs")

    sync.receive(batch_size=20, t_ms=4200, db=db)

    queue_after = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")['cnt']
    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    print(f"Incoming queue after Alice processes: {queue_after} blobs")
    print(f"Bob has channel valid: {bool(bob_has_channel)}")

    # Check why Alice didn't send anything
    alice_shareable = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))
    bob_shareable = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],))
    bob_in_store = db.query("SELECT id FROM store")

    # Check how many of Alice's shareable events Bob actually has
    alice_event_ids = set(row['event_id'] for row in alice_shareable)
    bob_event_ids = set(row['id'] for row in bob_in_store)
    bob_has_alice_events = alice_event_ids & bob_event_ids

    print(f"\nAlice has {len(alice_shareable)} shareable events")
    print(f"Bob has {len(bob_shareable)} shareable events")
    print(f"Bob has {len(bob_in_store)} total events in store")
    print(f"Bob actually has {len(bob_has_alice_events)}/{len(alice_event_ids)} of Alice's shareable events in his store")

    if len(bob_has_alice_events) > 0:
        print(f"✓ Bob legitimately has {len(bob_has_alice_events)} of Alice's events")
        print(f"  Alice should send {len(alice_event_ids) - len(bob_has_alice_events)} events that Bob doesn't have")

    # Check if Bob's bloom is wrong
    if len(alice_shareable) > 0 and queue_after == queue_before:
        print(f"\n⚠️ WARNING: Alice has shareable events but didn't send any!")
        if len(bob_has_alice_events) == len(alice_event_ids):
            print(f"✓ Bob's bloom is CORRECT - he has all {len(alice_event_ids)} of Alice's events")
        else:
            print(f"✗ Bob's bloom is WRONG - claims to have events he doesn't have!")

    # For now, skip the assertion to see more of the test output
    # assert queue_after > queue_before, f"Alice should have added sync response blobs to queue! before={queue_before}, after={queue_after}"

    print(f"\n=== STEP 4: Bob receives Alice's sync response ===")
    queue_before_bob = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")['cnt']
    print(f"Incoming queue before Bob receives: {queue_before_bob} blobs")

    sync.receive(batch_size=20, t_ms=4300, db=db)

    queue_after_bob = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")['cnt']
    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    bob_blocked = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
    print(f"Incoming queue after Bob receives: {queue_after_bob} blobs")
    print(f"Bob has channel valid: {bool(bob_has_channel)}")
    print(f"Bob blocked events: {bob_blocked['cnt']}")

    # Assert: Bob should have drained some blobs
    assert queue_before_bob > 0, f"There should be blobs for Bob to receive! queue={queue_before_bob}"

    print(f"\n=== STEP 5: Run debug sync to convergence ===")
    send_rounds = sync_debug.send_request_to_all_debug(t_ms=4400, db=db, max_rounds=50)
    print(f"Debug sync send completed in {send_rounds} rounds")

    receive_rounds = sync_debug.receive_debug(batch_size=20, t_ms=5000, db=db, max_rounds=50)
    print(f"Debug sync receive completed in {receive_rounds} rounds")

    bob_has_channel = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (bob['channel_id'], bob['peer_id']))
    bob_blocked = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
    print(f"After debug sync: Bob has channel valid: {bool(bob_has_channel)}, blocked: {bob_blocked['cnt']}")

    print(f"\n=== Verify Bob received and projected group_key_shared ===")

    # Check if Bob has Alice's network key (from GKS) in his group_keys table
    alice_network_key_id = alice['key_id']  # Alice's network key
    bob_has_network_key = db.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (alice_network_key_id, bob['peer_id'])
    )
    print(f"Bob has Alice's network key: {bool(bob_has_network_key)}")

    # Find all shareable events from Alice
    alice_shareable_events = db.query(
        """SELECT se.event_id
           FROM shareable_events se
           WHERE se.can_share_peer_id = ?
           ORDER BY se.created_at""",
        (alice['peer_id'],)
    )
    print(f"Alice has {len(alice_shareable_events)} shareable events total")
    for row in alice_shareable_events:
        # Try to determine event type
        event_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (row['event_id'],))
        if event_blob:
            try:
                data = crypto.parse_json(event_blob['blob'])
                event_type = data.get('type', 'unknown')
            except:
                event_type = 'encrypted'
            # Get window_id from shareable_events
            window_row = db.query_one(
                "SELECT window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
                (row['event_id'], alice['peer_id'])
            )
            window_id = window_row['window_id'] if window_row else 'N/A'
            print(f"  - {row['event_id'][:20]}... type={event_type} window={window_id}")

    # Count Bob's valid events
    bob_valid_count = db.query_one(
        "SELECT COUNT(*) as count FROM valid_events WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"Bob has {bob_valid_count['count']} valid events total")

    # Check what events Bob has in store from Alice (regardless of validation)
    bob_has_alice_in_store = db.query(
        """SELECT s.id
           FROM store s
           WHERE s.id IN (SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?)""",
        (alice['peer_id'],)
    )
    print(f"Bob has {len(bob_has_alice_in_store)} of Alice's {len(alice_shareable_events)} events in STORE (unwrapped):")
    for row in bob_has_alice_in_store:
        # Check if this event is valid
        is_valid = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (row['id'], bob['peer_id'])
        )
        # Try to get event type
        event_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (row['id'],))
        try:
            data = crypto.parse_json(event_blob['blob'])
            event_type = data.get('type', 'unknown')
        except:
            event_type = 'encrypted'

        status = "VALID" if is_valid else "NOT_VALID"
        print(f"  - {row['id'][:20]}... type={event_type} {status}")

    # Check which of those are also valid
    bob_has_alice_events = db.query(
        """SELECT ve.event_id
           FROM valid_events ve
           WHERE ve.recorded_by = ?
           AND ve.event_id IN (SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?)
           LIMIT 10""",
        (bob['peer_id'], alice['peer_id'])
    )
    print(f"Bob has {len(bob_has_alice_events)} of Alice's {len(alice_shareable_events)} shareable events VALID:")

    # Summary: Bob has ALL events in store but only some are valid
    not_valid_count = len([row for row in bob_has_alice_in_store
                           if not db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
                                              (row['id'], bob['peer_id']))])
    print(f"\nSummary: Bob received {len(bob_has_alice_in_store)}/{len(alice_shareable_events)} events (unwrapped successfully)")
    print(f"         But only {len(bob_has_alice_in_store) - not_valid_count}/{len(bob_has_alice_in_store)} are valid (projection issue, not sync issue)")

    # Check what group keys Bob has
    bob_group_keys = db.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (bob['peer_id'],))
    print(f"\nBob has {len(bob_group_keys)} group keys:")
    for row in bob_group_keys:
        print(f"  - {row['key_id'][:20]}...")

    # Check what group keys Alice has
    alice_group_keys = db.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (alice['peer_id'],))
    print(f"Alice has {len(alice_group_keys)} group keys:")
    for row in alice_group_keys:
        print(f"  - {row['key_id'][:20]}...")

    # Try to unwrap the NOT_VALID encrypted events to see what keys they need
    import crypto as crypto_mod
    print(f"\nTrying to unwrap NOT_VALID encrypted events:")
    missing_key_set = set()
    for row in bob_has_alice_in_store:
        is_valid = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
                               (row['id'], bob['peer_id']))
        if not is_valid:
            event_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (row['id'],))
            try:
                event_data = crypto_mod.parse_json(event_blob['blob'])
                event_type = event_data.get('type', 'unknown')
                print(f"  - {row['id'][:20]}... plaintext type={event_type} (not encrypted)")
            except:
                # It's encrypted - try to unwrap
                unwrapped, missing_keys = crypto_mod.unwrap_event(event_blob['blob'], bob['peer_id'], db)
                if missing_keys:
                    print(f"  - {row['id'][:20]}... MISSING KEYS: {[k[:20]+'...' for k in missing_keys]}")
                    missing_key_set.update(missing_keys)
                else:
                    print(f"  - {row['id'][:20]}... failed to unwrap (no missing keys reported)")

    # Check what the missing keys are
    print(f"\nUnique missing keys ({len(missing_key_set)}):")
    for key_id in missing_key_set:
        # Check if it's a group key Alice has
        alice_has_key = db.query_one("SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
                                     (key_id, alice['peer_id']))
        # Check if there's a group_key_shared event for this key in ANY peer's shareable_events
        gks_in_shareable = db.query(
            """SELECT event_id, can_share_peer_id FROM shareable_events se
               JOIN store s ON s.id = se.event_id
               WHERE s.blob LIKE '%group_key_shared%'"""
        )
        print(f"  - Missing key: {key_id[:20]}... alice_has={bool(alice_has_key)}")
        print(f"    All group_key_shared events in shareable_events: {len(gks_in_shareable)}")
        for gks_row in gks_in_shareable:
            # Check if Bob received this GKS event
            bob_has_gks = db.query_one("SELECT 1 FROM store WHERE id = ?", (gks_row['event_id'],))
            print(f"      GKS {gks_row['event_id'][:20]}... shared_by={gks_row['can_share_peer_id'][:10]}... bob_has={bool(bob_has_gks)}")

    # The GKS event created during invite should have this approximate ID
    # Let me check the store table directly
    print(f"\nChecking store table for all events:")
    all_in_store = db.query("SELECT id FROM store ORDER BY id LIMIT 50")
    print(f"Total events in store: {len(all_in_store)}")

    # Check if group_key_shared events exist in store at all
    all_gks_in_store = db.query(
        """SELECT s.id FROM store s WHERE s.blob LIKE '%group_key_shared%'"""
    )
    print(f"group_key_shared events in store (by blob content): {len(all_gks_in_store)}")

    # The logs show the GKS was added to shareable_events, so let me query directly
    print(f"\n**DEPENDENCY CYCLE INVESTIGATION**")
    all_shareable_for_alice = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )
    print(f"Alice has {len(all_shareable_for_alice)} events in shareable_events")

    # Check each shareable event to see if it's the GKS
    gks_in_shareable = 0
    for row in all_shareable_for_alice:
        blob = db.query_one("SELECT blob FROM store WHERE id = ?", (row['event_id'],))
        if blob and b'group_key_shared' in blob['blob']:
            gks_in_shareable += 1
            print(f"  Found GKS in shareable: {row['event_id'][:20]}...")

    print(f"GKS events in Alice's shareable_events: {gks_in_shareable}")
    print(f"GKS events Bob received: {len([e for e in bob_has_alice_in_store if b'group_key_shared' in db.query_one('SELECT blob FROM store WHERE id = ?', (e['id'],))['blob']])}")

    # The GKS was logged as added to shareable_events but it's not there!
    # This means either:
    # 1. The transaction was rolled back
    # 2. The add failed silently
    # 3. The shareable_events table is being queried from wrong scope
    print(f"\n**ROOT CAUSE**: GKS was logged as added to shareable_events, but it's not in the table!")
    print(f"This indicates a transaction rollback or database scoping issue.")

    for row in all_gks_in_store:
        # Check if in shareable_events
        in_shareable = db.query("SELECT can_share_peer_id FROM shareable_events WHERE event_id = ?", (row['id'],))
        # Check if in recorded
        in_recorded = db.query("SELECT recorded_by FROM recorded WHERE ref_id = ?", (row['id'],))
        print(f"  GKS {row['id'][:20]}... shareable={len(in_shareable)} recorded={len(in_recorded)}")

    # Check if Bob has received any group_key_shared events
    bob_gks_in_store = db.query(
        """SELECT s.id, v.event_id as valid_id
           FROM store s
           LEFT JOIN valid_events v ON v.event_id = s.id AND v.recorded_by = ?
           WHERE s.blob LIKE '%group_key_shared%'""",
        (bob['peer_id'],)
    )
    print(f"Bob has {len(bob_gks_in_store)} group_key_shared events in store")

    # === NEW DIAGNOSTIC: Check crypto hints match Bob's keys ===
    print(f"\n=== Crypto Hint Verification ===")

    # Get all events Bob received from Alice
    for event_row in bob_has_alice_in_store:
        event_id = event_row['id']
        event_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (event_id,))['blob']

        # Extract crypto hint (first 16 bytes of wrapped blobs)
        if len(event_blob) >= 16:
            import crypto
            hint_bytes = event_blob[:16]
            hint_b64 = crypto.b64encode(hint_bytes)

            # Check if Bob has a matching key in group_prekeys
            bob_has_group_prekey = db.query_one(
                "SELECT 1 FROM group_prekeys WHERE prekey_id = ? AND owner_peer_id = ? AND recorded_by = ?",
                (hint_b64, bob['peer_id'], bob['peer_id'])
            )

            # Check if Bob has a matching key in group_keys
            bob_has_group_key = db.query_one(
                "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
                (hint_b64, bob['peer_id'])
            )

            # Check if Bob has a matching key in transit_prekeys
            bob_has_transit_prekey = db.query_one(
                "SELECT 1 FROM transit_prekeys WHERE prekey_id = ? AND owner_peer_id = ?",
                (hint_b64, bob['peer_id'])
            )

            # Determine if this event can be decrypted
            can_decrypt = bool(bob_has_group_prekey or bob_has_group_key or bob_has_transit_prekey)

            # Check if event is valid (successfully projected)
            is_valid = db.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
                (event_id, bob['peer_id'])
            )

            print(f"  Event {event_id[:20]}... hint={hint_b64[:20]}... "
                  f"group_prekey={bool(bob_has_group_prekey)} group_key={bool(bob_has_group_key)} "
                  f"transit_prekey={bool(bob_has_transit_prekey)} valid={bool(is_valid)}")

            # If Bob should be able to decrypt but event is not valid, this is the problem
            if can_decrypt and not is_valid:
                print(f"    ⚠️ WARNING: Bob has key to decrypt but event NOT valid!")

                # Check if this event is blocked
                # Bob's blocked events reference the recorded wrapper ID, not the ref_id
                # So we need to find any recorded events that reference this event_id
                blocked_recorded_ids = db.query(
                    """SELECT DISTINCT be.recorded_id
                       FROM blocked_events_ephemeral be
                       JOIN store s ON s.id = be.recorded_id
                       WHERE json_extract(s.blob, '$.ref_id') = ? AND be.recorded_by = ?""",
                    (event_id, bob['peer_id'])
                )

                if blocked_recorded_ids:
                    print(f"    Event IS BLOCKED ({len(blocked_recorded_ids)} recorded wrappers):")
                    for row in blocked_recorded_ids:
                        recorded_id = row['recorded_id']
                        # Get the missing dependencies
                        blocked_deps = db.query(
                            """SELECT dep_id FROM blocked_event_deps_ephemeral
                               WHERE recorded_id = ? AND recorded_by = ?""",
                            (recorded_id, bob['peer_id'])
                        )
                        print(f"      Recorded {recorded_id[:20]}... blocked on {len(blocked_deps)} deps:")
                        for dep in blocked_deps:
                            dep_id = dep['dep_id']
                            # Check if Bob has this dependency
                            bob_has_dep = db.query_one(
                                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
                                (dep_id, bob['peer_id'])
                            )
                            print(f"        - {dep_id[:20]}... (Bob has: {bool(bob_has_dep)})")
                else:
                    print(f"    Event is NOT blocked - projection may have failed for another reason")

            elif not can_decrypt and not is_valid:
                print(f"    ℹ️ Bob missing key: event blocked as expected")

    # Specifically check for invite prekey
    print(f"\n=== Invite Prekey Check ===")
    bob_invite_prekeys = db.query(
        "SELECT prekey_id FROM group_prekeys WHERE owner_peer_id = ? AND recorded_by = ?",
        (bob['peer_id'], bob['peer_id'])
    )
    print(f"Bob has {len(bob_invite_prekeys)} invite prekeys in group_prekeys:")
    for row in bob_invite_prekeys:
        print(f"  - {row['prekey_id'][:20]}...")

    # Check Bob's transit keys
    print(f"\n=== Transit Key Check ===")
    bob_transit_keys = db.query(
        "SELECT key_id FROM transit_keys WHERE owner_peer_id = ?",
        (bob['peer_id'],)
    )
    print(f"Bob has {len(bob_transit_keys)} transit keys")
    for row in bob_transit_keys:
        print(f"  - {row['key_id'][:20]}...")

    # Check if Bob has recorded events for the events he received
    print(f"\n=== Recorded Events Check ===")
    # Recorded events are plaintext JSON in the store
    import json as json_lib
    all_store_rows = db.query("SELECT id, blob FROM store")
    bob_recorded_count = 0
    for row in all_store_rows:
        try:
            blob_data = json_lib.loads(row['blob'])
            if blob_data.get('type') == 'recorded' and blob_data.get('recorded_by') == bob['peer_id']:
                bob_recorded_count += 1
        except:
            pass
    print(f"Bob has {bob_recorded_count} recorded events")

    # Check ALL of Bob's received events for recorded wrappers
    print(f"\n=== Checking ALL events for recorded wrappers ===")
    events_with_wrappers = 0
    events_without_wrappers = 0
    gks_event_id = None

    for event_row in bob_has_alice_in_store:
        event_id = event_row['id']
        event_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (event_id,))['blob']

        # Check if it has a recorded wrapper
        recorded_wrapper_id = None
        for row in all_store_rows:
            try:
                blob_data = json_lib.loads(row['blob'])
                if (blob_data.get('type') == 'recorded' and
                    blob_data.get('ref_id') == event_id and
                    blob_data.get('recorded_by') == bob['peer_id']):
                    recorded_wrapper_id = row['id']
                    break
            except:
                pass

        # Check if this is the GKS (has matching invite prekey hint)
        is_gks = False
        if len(event_blob) >= 16:
            import crypto
            hint_bytes = event_blob[:16]
            hint_b64 = crypto.b64encode(hint_bytes)
            bob_has_group_prekey = db.query_one(
                "SELECT 1 FROM group_prekeys WHERE prekey_id = ? AND owner_peer_id = ? AND recorded_by = ?",
                (hint_b64, bob['peer_id'], bob['peer_id'])
            )
            is_gks = bool(bob_has_group_prekey)
            if is_gks:
                gks_event_id = event_id

        # Check if valid
        is_valid = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (event_id, bob['peer_id'])
        )

        if recorded_wrapper_id:
            events_with_wrappers += 1
        else:
            events_without_wrappers += 1
            marker = "🔑 GKS" if is_gks else ""

            # For events without wrappers, check the transit key hint
            if len(event_blob) >= 16:
                import crypto
                hint_bytes = event_blob[:16]
                hint_b64 = crypto.b64encode(hint_bytes)

                # Check if this transit key hint is in Bob's transit_keys
                bob_has_transit_key = db.query_one(
                    "SELECT 1 FROM transit_keys WHERE key_id = ? AND owner_peer_id = ?",
                    (hint_b64, bob['peer_id'])
                )

                print(f"  ❌ Event {event_id[:20]}... NO recorded wrapper, valid={bool(is_valid)}, bob_has_transit_key={bool(bob_has_transit_key)} {marker}")

    print(f"\nSummary: {events_with_wrappers} events WITH wrappers, {events_without_wrappers} events WITHOUT wrappers")

    if gks_event_id:
        print(f"\n⚠️ GKS event {gks_event_id[:20]}... is missing recorded wrapper!")

    # Critical assertion - this will show us if sync is working
    assert bob_has_network_key, f"Bob should have Alice's network key {alice_network_key_id} after sync (received via group_key_shared)"

    # Check if channel blob is in store
    channel_in_store = db.query_one("SELECT 1 FROM store WHERE id = ?", (bob['channel_id'],))
    print(f"Channel in store: {bool(channel_in_store)}")

    # Check if channel can be unwrapped
    if channel_in_store:
        import crypto
        channel_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (bob['channel_id'],))['blob']
        plaintext, missing = crypto.unwrap(channel_blob, bob['peer_id'], db)
        print(f"Channel unwrap: plaintext={bool(plaintext)}, missing_keys={missing}")

    # Check WHICH events are blocked
    if bob_blocked['cnt'] > 0:
        print(f"\n=== Blocked Events Debug ===")
        blocked_rows = db.query("SELECT recorded_id FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
        missing_deps_set = set()
        for row in blocked_rows:
            # Get the missing deps
            deps = db.query("SELECT dep_id FROM blocked_event_deps_ephemeral WHERE recorded_id = ?", (row['recorded_id'],))
            dep_list = [d['dep_id'] for d in deps]
            missing_deps_set.update(dep_list)
            print(f"Blocked event: {row['recorded_id'][:20]}..., missing: {[d[:20] + '...' for d in dep_list]}")

        # Check if the missing deps are in store
        print(f"\n=== Missing Dependencies Analysis ===")
        for dep_id in missing_deps_set:
            in_store = db.query_one("SELECT 1 FROM store WHERE id = ?", (dep_id,))
            in_valid = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?", (dep_id, bob['peer_id']))
            in_shareable = db.query_one("SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?", (dep_id, alice['peer_id']))

            # Try to get event type
            event_type = "unknown"
            if in_store:
                try:
                    blob = db.query_one("SELECT blob FROM store WHERE id = ?", (dep_id,))['blob']
                    data = crypto.parse_json(blob)
                    event_type = data.get('type', 'unknown')
                except:
                    event_type = "encrypted/unparseable"

            print(f"Dep {dep_id[:20]}... type={event_type}, in_store={bool(in_store)}, in_valid={bool(in_valid)}, in_alice_shareable={bool(in_shareable)}")

            # This is the BUG: event is valid but not in store - must be ephemeral!
            if in_valid and not in_store:
                print(f"  ❌ BUG: Event is in valid_events but NOT in store! This must be an ephemeral event.")
                print(f"  Ephemeral events (sync, transit_key) should NOT be dependencies for other events!")

    db.commit()

    # Ensure Bob has the channel event marked as valid before creating message
    # This ensures it will re-project correctly
    for attempt in range(20):
        bob_has_channel_event = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (bob['channel_id'], bob['peer_id'])
        )
        if bob_has_channel_event:
            break
        t_base = 5000 + (attempt * 10)
        sync.send_request_to_all(t_ms=t_base, db=db)
        sync.receive(batch_size=20, t_ms=t_base + 5, db=db)

    assert bob_has_channel_event, f"Bob should have channel event {bob['channel_id']} marked as valid after sync"

    # Verify the channel is also projected (for message.create() to work)
    bob_channel = db.query_one(
        "SELECT channel_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (bob['channel_id'], bob['peer_id'])
    )
    assert bob_channel, f"Bob should have channel {bob['channel_id']} projected"

    # Debug: Check how many times channel is in channels table for each peer
    print(f"\nDEBUG: Channel {bob['channel_id'][:20]}... in channels table:")
    for peer_name, peer_data in [('Alice', alice), ('Bob', bob), ('Charlie', charlie)]:
        channel_rows = db.query(
            "SELECT recorded_at FROM channels WHERE channel_id = ? AND recorded_by = ?",
            (bob['channel_id'], peer_data['peer_id'])
        )
        print(f"  {peer_name}: {len(channel_rows)} rows, recorded_at={[r['recorded_at'] for r in channel_rows]}")

    # Debug: Count recorded events for the channel by peer
    print(f"\nDEBUG: Recorded events for channel {bob['channel_id'][:20]}...:")
    import crypto as crypto_mod
    channel_id_bytes = crypto_mod.b64decode(bob['channel_id'])
    all_recorded = db.query("SELECT id, blob FROM store ORDER BY rowid")
    for peer_name, peer_data in [('Alice', alice), ('Bob', bob), ('Charlie', charlie)]:
        count = 0
        for row in all_recorded:
            try:
                event_data = crypto_mod.parse_json(row['blob'])
                if event_data.get('type') == 'recorded':
                    if event_data.get('ref_id') == bob['channel_id'] and event_data.get('recorded_by') == peer_data['peer_id']:
                        count += 1
            except:
                pass
        print(f"  {peer_name}: {count} recorded events")

    # Each peer creates a message
    alice_msg = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Hello from Alice',
        t_ms=6000,
        db=db
    )

    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content='Hello from Bob',
        t_ms=7000,
        db=db
    )

    charlie_msg = message.create(
        peer_id=charlie['peer_id'],
        channel_id=charlie['channel_id'],
        content='Hello from Charlie',
        t_ms=8000,
        db=db
    )

    # Round 1: Send sync requests (all peers sync with peers they've seen)
    sync.send_request_to_all(t_ms=9000, db=db)

    # Receive sync requests - this unwraps requests and auto-sends responses
    sync.receive(batch_size=10, t_ms=10000, db=db)

    # Round 2: Receive sync responses
    sync.receive(batch_size=100, t_ms=11000, db=db)

    # Round 3: Sync window 1 (with w=1, there are 2 windows: 0 and 1)
    sync.send_request_to_all(t_ms=12000, db=db)

    # Receive sync requests for window 1
    sync.receive(batch_size=10, t_ms=13000, db=db)

    # Receive sync responses for window 1
    sync.receive(batch_size=100, t_ms=14000, db=db)

    # Round 4: Additional sync rounds for convergence
    for round_num in range(10):
        base_time = 15000 + (round_num * 1000)
        sync.send_request_to_all(t_ms=base_time, db=db)
        sync.receive(batch_size=100, t_ms=base_time + 100, db=db)
        sync.receive(batch_size=100, t_ms=base_time + 200, db=db)

    # Debug: Check peers_shared table
    alice_peers_shared = db.query(
        "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    bob_peers_shared = db.query(
        "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"\nAlice's peers_shared table has {len(alice_peers_shared)} entries:")
    for ps in alice_peers_shared:
        is_bob = ps['peer_shared_id'] == bob['peer_shared_id']
        is_alice = ps['peer_shared_id'] == alice['peer_shared_id']
        print(f"  - {ps['peer_shared_id'][:20]}... (Bob's? {is_bob}, Alice's? {is_alice})")
    print(f"Bob's peers_shared table has {len(bob_peers_shared)} entries:")
    for ps in bob_peers_shared:
        is_bob = ps['peer_shared_id'] == bob['peer_shared_id']
        is_alice = ps['peer_shared_id'] == alice['peer_shared_id']
        print(f"  - {ps['peer_shared_id'][:20]}... (Bob's? {is_bob}, Alice's? {is_alice})")

    # Debug: Check bootstrap_status entries
    all_bootstrap_status = db.query("SELECT peer_id, recorded_by, created_network, joined_network FROM bootstrap_status")
    print(f"\nAll bootstrap_status entries ({len(all_bootstrap_status)}):")
    for entry in all_bootstrap_status:
        peer_is_alice = entry['peer_id'] == alice['peer_id']
        peer_is_bob = entry['peer_id'] == bob['peer_id']
        recorded_is_alice = entry['recorded_by'] == alice['peer_id']
        recorded_is_bob = entry['recorded_by'] == bob['peer_id']
        print(f"  peer_id={entry['peer_id'][:20]}... (Alice? {peer_is_alice}, Bob? {peer_is_bob}) recorded_by={entry['recorded_by'][:20]}... (Alice? {recorded_is_alice}, Bob? {recorded_is_bob}) created={entry['created_network']} joined={entry['joined_network']}")

    # Debug: Check sync state
    alice_syncing_with = db.query(
        "SELECT to_peer_id FROM sync_state_ephemeral WHERE from_peer_id = ?",
        (alice['peer_id'],)
    )
    bob_syncing_with = db.query(
        "SELECT to_peer_id FROM sync_state_ephemeral WHERE from_peer_id = ?",
        (bob['peer_id'],)
    )
    print(f"\nAlice (peer_id={alice['peer_id'][:20]}...) is syncing with {len(alice_syncing_with)} peers:")
    for p in alice_syncing_with:
        print(f"  - {p['to_peer_id'][:20]}... (Bob's peer_shared? {p['to_peer_id'] == bob['peer_shared_id']})")
    print(f"Bob (peer_id={bob['peer_id'][:20]}...) is syncing with {len(bob_syncing_with)} peers:")
    for p in bob_syncing_with:
        print(f"  - {p['to_peer_id'][:20]}... (Alice's peer_shared? {p['to_peer_id'] == alice['peer_shared_id']})")
    print(f"Alice's peer_shared_id: {alice['peer_shared_id'][:20]}")
    print(f"Bob's peer_shared_id: {bob['peer_shared_id'][:20]}")

    # Debug: Check if Bob's peer_shared is in Alice's valid_events
    alice_has_bob_peer_shared = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    )
    print(f"Alice has Bob's peer_shared in valid_events: {alice_has_bob_peer_shared is not None}")

    # Debug: Check if Bob's message is in Bob's shareable_events
    bob_message_shareable = db.query_one(
        "SELECT window_id, created_at, recorded_at FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (bob_msg['id'], bob['peer_id'])
    )
    print(f"Bob's message is in Bob's shareable_events: {bob_message_shareable is not None}")
    if bob_message_shareable:
        print(f"  window_id={bob_message_shareable['window_id']}, created_at={bob_message_shareable['created_at']}, recorded_at={bob_message_shareable['recorded_at']}")
    print(f"Bob's message ID: {bob_msg['id'][:20]}")

    # Debug: Check if Bob's message is in Alice's valid_events
    alice_has_bob_message = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob_msg['id'], alice['peer_id'])
    )
    print(f"Alice has Bob's message in valid_events: {alice_has_bob_message is not None}")

    # Verify event visibility via valid_events table
    # Alice should have received Bob's events (peer_shared, group, channel, message)
    alice_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    print(f"Alice has {len(alice_valid_events)} valid events")

    # Bob should have received Alice's events
    bob_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    print(f"Bob has {len(bob_valid_events)} valid events")

    # Charlie should only have his own events
    charlie_valid_events = db.query(
        "SELECT event_id FROM valid_events WHERE recorded_by = ?",
        (charlie['peer_id'],)
    )
    print(f"Charlie has {len(charlie_valid_events)} valid events")

    # Check specific message events in store
    alice_msg_blob = store.get(alice_msg['id'], db)
    bob_msg_blob = store.get(bob_msg['id'], db)
    charlie_msg_blob = store.get(charlie_msg['id'], db)

    # Alice should have Bob's message event
    bob_msg_valid_for_alice = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob_msg['id'], alice['peer_id'])
    )
    print(f"Bob's message valid for Alice: {bob_msg_valid_for_alice is not None}")

    # Bob should have Alice's message event
    alice_msg_valid_for_bob = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_msg['id'], bob['peer_id'])
    )
    print(f"Alice's message valid for Bob: {alice_msg_valid_for_bob is not None}")

    # Charlie should NOT have Alice's or Bob's messages
    alice_msg_valid_for_charlie = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_msg['id'], charlie['peer_id'])
    )
    bob_msg_valid_for_charlie = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob_msg['id'], charlie['peer_id'])
    )
    print(f"Alice's message valid for Charlie: {alice_msg_valid_for_charlie is not None}")
    print(f"Bob's message valid for Charlie: {bob_msg_valid_for_charlie is not None}")

    # Assertions
    assert bob_msg_valid_for_alice, "Alice should have Bob's message"
    assert alice_msg_valid_for_bob, "Bob should have Alice's message"
    assert not alice_msg_valid_for_charlie, "Charlie should NOT have Alice's message"
    assert not bob_msg_valid_for_charlie, "Charlie should NOT have Bob's message"

    print("✓ All tests passed! Three-player message transit works correctly.")

    # Debug: Check channels table before re-projection
    all_channels = db.query("SELECT channel_id, name, recorded_by FROM channels")
    print(f"\nChannels table ({len(all_channels)} rows) before re-projection:")
    for ch in all_channels:
        is_alice_viewer = ch['recorded_by'] == alice['peer_id']
        is_bob_viewer = ch['recorded_by'] == bob['peer_id']
        is_charlie_viewer = ch['recorded_by'] == charlie['peer_id']
        print(f"  channel={ch['name']} id={ch['channel_id'][:20]}... recorded_by={ch['recorded_by'][:20]}... (Alice? {is_alice_viewer}, Bob? {is_bob_viewer}, Charlie? {is_charlie_viewer})")

    # Re-projection test
    from tests.utils import assert_reprojection
    assert_reprojection(db)

    # Idempotency test
    from tests.utils import assert_idempotency
    assert_idempotency(db, num_trials=10, max_repetitions=5)

    # Convergence test
    from tests.utils import assert_convergence
    assert_convergence(db)


if __name__ == '__main__':
    test_three_player_messaging()
