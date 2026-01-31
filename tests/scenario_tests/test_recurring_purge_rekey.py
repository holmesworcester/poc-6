"""Test recurring purge and rekey operations via tick() - SKIPPED.

Tests that:
1. tick() runs message rekey/purge cycle for forward secrecy
2. tick() runs purge_expired to remove expired events
3. End-to-end: delete message -> tick() -> old key purged, message rekeyed
4. End-to-end: TTL expires -> tick() -> expired events removed

In the sender key model:
- group_prekey was replaced by pubkey/pubkey_shared
- Key rotation now uses TreeKEM instead of group key rotation
- Forward secrecy mechanism changed significantly

These tests need to be rewritten for the sender key model.
"""
import pytest


@pytest.mark.skip(reason="group_prekey removed in sender key model - needs rewrite")
def test_tick_runs_message_rekey_and_purge(fresh_db):
    """SKIPPED: group_prekey removed in sender key model."""
    pass


@pytest.mark.skip(reason="group_prekey removed in sender key model - needs rewrite")
def test_tick_runs_purge_expired(fresh_db):
    """SKIPPED: group_prekey removed in sender key model."""
    pass
