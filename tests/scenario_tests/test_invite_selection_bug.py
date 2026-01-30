"""
Scenario test: SKIPPED - Tests removed group_prekey/group_key functionality.

In the sender key model:
- Each sender has their own key (no shared group keys)
- group_prekey was replaced by pubkey/pubkey_shared
- Keys are distributed via TreeKEM, not sealed to invite prekeys

The original bug (time-based heuristic for invite selection) no longer applies
because the key sealing mechanism was completely replaced.
"""
import pytest


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_device_joins_with_older_invite_gets_correct_keys(fresh_db):
    """SKIPPED: group_prekey model removed in sender key architecture."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_multiple_invites_keys_sealed_to_correct_prekey(fresh_db):
    """SKIPPED: group_prekey model removed in sender key architecture."""
    pass
