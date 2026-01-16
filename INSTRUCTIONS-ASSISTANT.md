Prompt for external assistant

You’re jumping into /home/holmes/functional-projectors-and-commands (branch: functional-projectors-and-commands). Master must stay clean. Work in this worktree only.

Issue
- After v2 conversion of peer_shared + channel, device linking scenario fails.
- Repro:
  PYTHONPATH=/home/holmes/functional-projectors-and-commands pytest -n 0 tests/scenario_tests/test_multi_device_linking.py::test_alice_links_phone_to_laptop -vv -s
- Failure: laptop never gets group key; assertion fails at “Laptop should have Alice’s group key after sync”.

Recent context
- peer_shared v2 conversion in /home/holmes/functional-projectors-and-commands/events/identity/peer_shared.py with EVENT_SPEC (requires invite), project_pure, signer_type=invite, and recorded.py hook to _seed_group_keys_for_linked_device.
- channel v2 conversion in /home/holmes/functional-projectors-and-commands/events/content/channel.py.
- recorded.py v2 path used for these events.
- Fix for invites table: /home/holmes/functional-projectors-and-commands/events/identity/invite.py now sets group_id = '' on projection if missing (invites table has group_id NOT NULL). Without this, peer_shared was rejected because invite row missing.
- Tests: parity tests for message, channel, peer_shared pass. Scenario test still fails (group key not delivered).

Debug observations
- Phone has group_keys_shared row for all_users key with recipient_prekey_id == invite_prekey_id from the device link invite.
- Phone has shareable_events entry for that group_key_shared event.
- Laptop has group_prekeys including invite_prekey_id (from invite.accept/create_from_material), but group_keys_shared and group_keys are empty.
- Laptop doesn’t have that gks event in its shareable_events.
- Log seen during bootstrap: group_member.create() failed to share key ... No group prekey found for recipient peer: <peer_shared_id> (might be relevant).

Task
- Diagnose why group_key_shared isn’t reaching/decrypting on laptop after sync.
- Focus on sync pipeline: send_request/send_response/connection handshake, shareable_events selection, routing.
- Check /home/holmes/functional-projectors-and-commands/events/network/sync.py, /home/holmes/functional-projectors-and-commands/core/crypto.py (unwrap_event/get_event_key_by_id), /home/holmes/functional-projectors-and-commands/core/db.py (get_shareable_blob), and anything around connections.
- Determine whether laptop is actually sending sync requests to phone (peer_shared discovery in send_request_to_all), whether phone is responding with the gks event, and whether laptop can route/decrypt the received blob.
- Propose a minimal fix and run the targeted test above after each change (tests required for each change).
- Avoid slow huge tests (10k messages / big file download).

Report back with findings, recommended fix, and any patch to apply.
