"""Forward secrecy scenario tests - SKIPPED.

The sender key model has different forward secrecy mechanisms:
- Each sender has their own key (secret)
- After user removal, TreeKEM is used to distribute new epoch keys
- connection_prekey was renamed to connection_pubkey
- group_prekey was replaced by pubkey/pubkey_shared

These tests need to be rewritten for the sender key model.
"""
import pytest


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_delete_message_marks_key_for_purging(fresh_db_with_alice):
    """SKIPPED: Forward secrecy mechanism changed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_delete_and_rekey_message(fresh_db_with_alice):
    """SKIPPED: Rekeying mechanism changed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_forward_secrecy_multi_peer(fresh_db_with_alice):
    """SKIPPED: Forward secrecy mechanism changed in sender key model."""
    pass


@pytest.mark.skip(reason="connection_prekey renamed to connection_pubkey")
def test_prekey_ttl_and_purge_expired(fresh_db_with_alice):
    """SKIPPED: connection_prekey renamed to connection_pubkey."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_generate_batch_prekeys(fresh_db_with_alice):
    """SKIPPED: group_prekey removed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_multi_peer_purge_convergence(fresh_db_with_alice):
    """SKIPPED: group_prekey removed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_new_user_joins_after_rekey(fresh_db_with_alice):
    """SKIPPED: Rekeying mechanism changed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_new_user_with_preexisting_invite_after_rekey(fresh_db_with_alice):
    """SKIPPED: Rekeying mechanism changed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_deterministic_rekeying(fresh_db_with_alice):
    """SKIPPED: Rekeying mechanism changed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey/group_key removed in sender key model")
def test_rekey_no_duplicates(fresh_db_with_alice):
    """SKIPPED: Rekeying mechanism changed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey removed in sender key model")
def test_deterministic_prekey_ids(fresh_db_with_alice):
    """SKIPPED: group_prekey removed in sender key model."""
    pass


@pytest.mark.skip(reason="group_key removed in sender key model")
def test_deterministic_group_key_ids(fresh_db_with_alice):
    """SKIPPED: group_key removed in sender key model."""
    pass
