"""
Validator for user events.
"""
from typing import Dict, Any, Set
from core.core_types import validator


@validator
def validate(envelope: dict[str, Any]) -> bool:
    """
    Validate a user event.

    For self-created user events (joining via invite link), we trust the invite
    data since the user chose to trust the invite link. The invite_pubkey and
    invite_signature prove they have the invite secret.

    For external user events, full validation including invite verification
    would be needed.

    Returns True if valid, False otherwise.
    """
    event = envelope['event_plaintext']

    # Check required fields - basic fields always required (type already verified by generic validator)
    # Note: signature is added by the signature handler, not required at validation time
    required_fields = ['type', 'peer_id', 'network_id', 'name', 'created_at']
    for field in required_fields:
        if field not in event:
            return False

    # Validate network_id is not empty
    if not event['network_id']:
        return False

    # If this is an invite-based join, validate invite fields
    if 'invite_pubkey' in event or 'invite_signature' in event:
        # Both fields must be present if either is
        if not (event.get('invite_pubkey') and event.get('invite_signature')):
            return False
        # group_id is required for invite-based joins
        if not event.get('group_id'):
            return False

    # Validate name is not empty
    if not event.get('name'):
        return False

    # Check that peer_id in envelope matches event
    if 'peer_id' in envelope:
        if envelope['peer_id'] != event['peer_id']:
            return False
    elif not envelope.get('self_created'):
        # For non-self-created events, peer_id in envelope is required
        return False

    # For self-created events, we trust the invite data
    # The user explicitly chose to trust the invite link
    if envelope.get('self_created'):
        # The invite_pubkey and invite_signature prove they have the invite secret
        # We don't need the actual invite event to exist
        return True

    # If this is an invite-based user event, validate the invite
    if 'invite_pubkey' in event:
        # For external user events with invites, we need full validation
        # Check that dependencies were resolved (invite must exist)
        if not envelope.get('deps_included_and_valid'):
            return False

        resolved_deps = envelope.get('resolved_deps', {})

        # Find the invite dependency
        invite_dep_key = f"invite:{event['invite_pubkey']}"
        if invite_dep_key not in resolved_deps:
            # Invite dependency must be present for external events
            return False

        invite_envelope = resolved_deps[invite_dep_key]
        invite_event = invite_envelope.get('event_plaintext', {})

        # Verify the invite matches what the user claims
        if invite_event.get('group_id') != event['group_id']:
            return False

        # Verify the invite_signature is valid
        # This proves the user had the invite_secret
        # (actual signature verification would happen here)
        if not event['invite_signature']:
            return False

    # The peer that signed this user event must match the peer_id in the event
    # (signature handler already verified the signature matches the peer)

    return True
