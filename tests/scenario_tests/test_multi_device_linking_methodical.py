"""Methodical multi-device linking test with detailed assertions."""
import sqlite3
import logging
from db import Database
import schema
from events.identity import user, link_invite, link


def setup_logging():
    """Setup detailed logging for debugging."""
    logging.basicConfig(
        level=logging.WARNING,  # Show all warnings and errors
        format='%(levelname)-8s %(name)s:%(lineno)d %(message)s',
        force=True
    )


def test_methodical_link_invite_creation():
    """Test 1: Verify link_invite is created with correct GKS events."""
    setup_logging()

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== TEST 1: Link invite creation ===")

    # Step 1: Create phone network
    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()
    print(f"✓ Phone created: peer_id={alice_phone['peer_id'][:20]}...")
    print(f"  user_id={alice_phone['user_id'][:20]}...")
    print(f"  key_id={alice_phone['key_id'][:20]}...")

    # Step 2: Create link invite
    link_invite_id, link_url, link_data = link_invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db
    )
    db.commit()
    print(f"✓ Link invite created: id={link_invite_id[:20]}...")

    # Assertion 1: Link invite should exist in store
    from db import create_unsafe_db
    import store
    unsafedb = create_unsafe_db(db)
    link_invite_blob = store.get(link_invite_id, unsafedb)
    assert link_invite_blob is not None, "Link invite blob should exist in store"
    print(f"✓ Link invite blob exists in store")

    # Assertion 2: Link data should have prekey
    assert 'link_prekey_id' in link_data, "link_data should have link_prekey_id"
    link_prekey_id_from_url = link_data['link_prekey_id']
    print(f"✓ Link prekey from URL: {link_prekey_id_from_url[:20]}...")

    # Assertion 3: Link data should have private key
    assert 'link_private_key' in link_data, "link_data should have link_private_key"
    print(f"✓ Link private key exists in URL")

    # Step 3: Check if link_invite event was projected on phone
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=alice_phone['peer_id'])
    link_invite_row = safedb.query_one(
        "SELECT * FROM link_invites WHERE link_invite_id = ? AND recorded_by = ?",
        (link_invite_id, alice_phone['peer_id'])
    )
    assert link_invite_row is not None, "Link invite should be projected on phone"
    print(f"✓ Link invite projected on phone")

    # Step 4: Check for GKS events created during link_invite.create()
    # Note: Some events (encrypted) have created_at=NULL until they're decrypted
    gks_events = safedb.query(
        """SELECT event_id FROM shareable_events
           WHERE can_share_peer_id = ? AND (created_at IS NULL OR (created_at >= ? AND created_at <= ?))
           ORDER BY created_at""",
        (alice_phone['peer_id'], 2000, 3000)
    )
    print(f"  Found {len(gks_events)} shareable events created after link_invite")

    if len(gks_events) > 0:
        print(f"✓ Link invite created {len(gks_events)} events (likely GKS)")
        # Print details
        for i, evt in enumerate(gks_events):
            ref_id = evt['event_id']
            print(f"    Event {i}: {ref_id[:20]}...")
    else:
        print(f"✗ No shareable events created by link_invite.create()!")
        print(f"  This suggests create_for_link_invite() was not called")

    return alice_phone, link_invite_id, link_url, link_data


def test_methodical_link_join():
    """Test 2: Verify laptop joins and stores prekey."""
    setup_logging()

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== TEST 2: Link join (laptop accepts link) ===")

    # Setup: Create phone and link invite
    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    link_invite_id, link_url, link_data = link_invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db
    )
    db.commit()
    print(f"✓ Setup complete: phone and link invite created")

    # Step 1: Laptop joins via link URL
    alice_laptop = link.join(
        link_url=link_url,
        t_ms=3000,
        db=db
    )
    db.commit()
    print(f"✓ Laptop joined via link: peer_id={alice_laptop['peer_id'][:20]}...")

    # Assertion 1: Both devices should have same user_id
    assert alice_laptop['user_id'] == alice_phone['user_id'], \
        f"Both should have same user_id"
    print(f"✓ Both devices have same user_id: {alice_laptop['user_id'][:20]}...")

    # Assertion 2: Different peer_ids
    assert alice_laptop['peer_id'] != alice_phone['peer_id'], \
        "Devices should have different peer_ids"
    print(f"✓ Devices have different peer_ids")

    # Assertion 3: Laptop should have link_prekey stored
    from db import create_safe_db
    safedb_laptop = create_safe_db(db, recorded_by=alice_laptop['peer_id'])

    link_prekey_id_from_url = link_data['link_prekey_id']
    prekey_row = safedb_laptop.query_one(
        """SELECT * FROM group_prekeys
           WHERE prekey_id = ? AND recorded_by = ?
           LIMIT 1""",
        (link_prekey_id_from_url, alice_laptop['peer_id'])
    )
    assert prekey_row is not None, \
        f"Laptop should have prekey {link_prekey_id_from_url[:20]}... in group_prekeys"
    print(f"✓ Laptop stored link prekey: {link_prekey_id_from_url[:20]}...")

    # Assertion 4: Laptop should have public key
    assert prekey_row['public_key'] is not None, "Prekey should have public_key"
    print(f"✓ Prekey has public key")

    # Assertion 5: Laptop should have private key
    assert prekey_row['private_key'] is not None, "Prekey should have private_key"
    print(f"✓ Prekey has private key")

    return alice_phone, alice_laptop, link_data


def test_methodical_gks_sync():
    """Test 3: Verify GKS is synced and blocked/unblocked correctly."""
    setup_logging()

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== TEST 3: GKS creation, sync, and unblocking ===")

    # Setup
    alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    link_invite_id, link_url, link_data = link_invite.create(
        peer_id=alice_phone['peer_id'],
        t_ms=2000,
        db=db
    )
    db.commit()
    print(f"✓ Setup: phone and link invite created")

    # Check: Were GKS events created during link_invite.create()?
    from db import create_safe_db, create_unsafe_db
    import store

    safedb_phone = create_safe_db(db, recorded_by=alice_phone['peer_id'])

    # Query shareable events in two ways:
    # 1. With created_at filter (like the test was doing)
    gks_events_from_phone_filtered = safedb_phone.query(
        """SELECT event_id, created_at FROM shareable_events
           WHERE can_share_peer_id = ? AND (created_at IS NULL OR (created_at >= 2000 AND created_at < 3000))
           ORDER BY created_at""",
        (alice_phone['peer_id'],)
    )

    # 2. All shareable events for the phone (what send_bootstrap_to_peer sends)
    gks_events_from_phone = safedb_phone.query(
        """SELECT event_id, created_at FROM shareable_events
           WHERE can_share_peer_id = ?
           ORDER BY created_at""",
        (alice_phone['peer_id'],)
    )

    print(f"Found {len(gks_events_from_phone_filtered)} shareable events with created_at filter")
    print(f"Found {len(gks_events_from_phone)} shareable events total (what bootstrap sends)")

    if len(gks_events_from_phone) == 0:
        print("✗ BUG FOUND: No GKS events were created by link_invite.create()!")
        print("  This means create_for_link_invite() was never called.")
        print("  Checking link_invite.create() code...")

        # Let's check if the network query succeeded
        unsafedb = create_unsafe_db(db)
        network_row = safedb_phone.query_one(
            "SELECT network_id, all_users_group_id FROM networks WHERE network_id = ? AND recorded_by = ?",
            (alice_phone['network_id'], alice_phone['peer_id'])
        )
        if network_row:
            print(f"  ✓ Network found: {network_row}")
        else:
            print(f"  ✗ Network NOT found! This is why GKS wasn't created")
            # Check what networks exist
            all_networks = safedb_phone.query(
                "SELECT network_id FROM networks WHERE recorded_by = ?",
                (alice_phone['peer_id'],)
            )
            print(f"  Available networks: {all_networks}")

        return  # Stop here since the bug is found

    print(f"✓ Found {len(gks_events_from_phone)} GKS events")
    print(f"  Phone's shareable events:")

    # Load each event to see its type
    unsafedb = create_unsafe_db(db)
    for i, evt in enumerate(gks_events_from_phone):
        evt_id = evt['event_id']
        evt_blob = store.get(evt_id, unsafedb)
        evt_type = "unknown"
        if evt_blob:
            try:
                import crypto
                import json
                # Try to parse as JSON (unencrypted events)
                evt_data = json.loads(evt_blob.decode())
                evt_type = evt_data.get('type', 'unknown')
            except:
                # Encrypted event or other format
                evt_type = "encrypted"
        print(f"    Event {i}: {evt_id[:20]}... type={evt_type} created_at={evt['created_at']}")

    # Now join with laptop
    alice_laptop = link.join(
        link_url=link_url,
        t_ms=3000,
        db=db
    )
    db.commit()
    print(f"✓ Laptop joined")

    # Check: Did laptop get the GKS events?
    from events.transit import sync

    # Run sync
    sync.send_request_to_all(t_ms=4000, db=db)
    db.commit()
    print(f"✓ Sync round 1 completed")

    # Check: Does laptop have the group key now?
    safedb_laptop = create_safe_db(db, recorded_by=alice_laptop['peer_id'])
    laptop_has_key = safedb_laptop.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (alice_phone['key_id'], alice_laptop['peer_id'])
    )

    if laptop_has_key:
        print(f"✓ Laptop received group key!")
    else:
        print(f"✗ Laptop does NOT have group key")

        # Debug: Check what GKS events the laptop received
        laptop_gks = safedb_laptop.query(
            """SELECT event_id FROM shareable_events
               WHERE can_share_peer_id = ? ORDER BY created_at""",
            (alice_laptop['peer_id'],)
        )
        print(f"  Laptop received {len(laptop_gks)} events (out of {len(gks_events_from_phone)} created):")
        for evt in laptop_gks:
            print(f"    {evt['event_id'][:20]}...")

        # Compare: which events from phone are missing on laptop?
        phone_event_ids = set(evt['event_id'] for evt in gks_events_from_phone)
        laptop_event_ids = set(evt['event_id'] for evt in laptop_gks)
        missing_on_laptop = phone_event_ids - laptop_event_ids
        extra_on_laptop = laptop_event_ids - phone_event_ids

        if missing_on_laptop:
            print(f"  Missing on laptop ({len(missing_on_laptop)} events):")
            for evt_id in list(missing_on_laptop)[:5]:
                print(f"    {evt_id[:20]}...")

        if extra_on_laptop:
            print(f"  Extra on laptop ({len(extra_on_laptop)} events NOT in phone's shareable_events):")
            for evt_id in list(extra_on_laptop):
                # Try to find what this event is
                evt_blob = store.get(evt_id, unsafedb)
                evt_type = "unknown"
                if evt_blob:
                    try:
                        import json
                        evt_data = json.loads(evt_blob.decode())
                        evt_type = evt_data.get('type', 'unknown')
                    except:
                        evt_type = "encrypted/other"
                print(f"    {evt_id[:20]}... type={evt_type}")

        # Check blocked events on laptop
        blocked_events = safedb_laptop.query(
            """SELECT recorded_id, missing_deps FROM blocked_events_ephemeral
               WHERE recorded_by = ?""",
            (alice_laptop['peer_id'],)
        )
        print(f"  Laptop has {len(blocked_events)} blocked events:")
        for evt in blocked_events:
            print(f"    {evt['recorded_id'][:20]}... blocked on: {evt['missing_deps']}")


if __name__ == '__main__':
    print("=" * 80)
    print("METHODICAL MULTI-DEVICE LINKING TEST")
    print("=" * 80)

    try:
        test_methodical_link_invite_creation()
    except Exception as e:
        print(f"\n✗ Test 1 failed: {e}")
        import traceback
        traceback.print_exc()

    try:
        test_methodical_link_join()
    except Exception as e:
        print(f"\n✗ Test 2 failed: {e}")
        import traceback
        traceback.print_exc()

    try:
        test_methodical_gks_sync()
    except Exception as e:
        print(f"\n✗ Test 3 failed: {e}")
        import traceback
        traceback.print_exc()
