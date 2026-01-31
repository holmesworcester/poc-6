"""Forward secrecy CLI tests - SKIPPED.

The sender key model has different forward secrecy mechanisms:
- group_key was removed (replaced by sender-specific secrets)
- group_prekey was replaced by pubkey/pubkey_shared
- Key rotation now uses TreeKEM instead of group key rotation

These tests need to be rewritten for the sender key model.
"""
import pytest


@pytest.mark.skip(reason="group_key/group_prekey removed in sender key model")
def test_keys_display():
    """SKIPPED: group_key removed in sender key model."""
    pass


@pytest.mark.skip(reason="group_key/group_prekey removed in sender key model")
def test_delete_message_marks_key_for_purging():
    """SKIPPED: group_key removed in sender key model."""
    pass


@pytest.mark.skip(reason="group_key/group_prekey removed in sender key model")
def test_manual_purge_keys():
    """SKIPPED: group_key removed in sender key model."""
    pass


@pytest.mark.skip(reason="group_key/group_prekey removed in sender key model")
def test_automatic_purge_job():
    """SKIPPED: group_key removed in sender key model."""
    pass


@pytest.mark.skip(reason="group_key/group_prekey removed in sender key model")
def test_cascading_prekey_purge():
    """SKIPPED: group_key/group_prekey removed in sender key model."""
    pass
