"""
Check Outgoing handler - Validates outgoing messages have consistent addressing and no secrets.

From plan.md:
- Filter: `is_outgoing: true` AND `deps_included_and_valid: true` AND no `outgoing_checked`
- Validates:
  - address, peer, and user all match and are consistent
  - event_type is not a secret type (identity_secret, transit_secret, etc.)
- Output Type: Same with `outgoing_checked: true`
"""
from typing import Any, List
import sqlite3
from core.handlers import Handler

# Removed core.types import


def filter_func(envelope: dict[str, Any]) -> bool:
    """
    Process outgoing envelopes that have dependencies resolved but haven't been checked.
    """
    return (
        envelope.get('is_outgoing') is True and
        envelope.get('deps_included_and_valid') is True and
        envelope.get('outgoing_checked') is not True
    )


def handler(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """
    Validate outgoing envelope has consistent addressing and no secrets.
    
    Args:
        envelope: Outgoing envelope with resolved dependencies
        
    Returns:
        dict[str, Any] with outgoing_checked: true if valid, error if not, or None to drop
    """
    # Check if this is a secret event type that shouldn't be sent
    event_type = envelope.get('event_type', '')
    secret_types = ['key_secret', 'prekey_secret']
    
    if event_type in secret_types:
        # Drop secret events - they should never be sent over network
        envelope['error'] = f"Cannot send secret event type: {event_type}"
        return None
    
    # TODO: Implement actual validation logic
    # For now, stub implementation that approves all non-secret events
    
    resolved_deps = envelope.get('resolved_deps', {})
    
    # Extract addressing info from resolved dependencies
    # Would normally check:
    # 1. address_id resolves to valid dest_ip and dest_port
    # 2. peer_id matches the destination peer
    # 3. user_id is consistent with the peer's user
    # 4. transit_key_id is valid for this peer connection
    
    # Look for address dependency
    address_deps = [k for k in resolved_deps.keys() if k.startswith('address:')]
    if address_deps:
        address_data = resolved_deps[address_deps[0]]
        # Would extract dest_ip and dest_port from address_data
        envelope['dest_ip'] = address_data.get('dest_ip', '127.0.0.1')  # Stub
        envelope['dest_port'] = address_data.get('dest_port', 8080)  # Stub
    
    # Stub: Always mark as checked for now
    envelope['outgoing_checked'] = True
    
    # If validation failed, we would set an error instead:
    # envelope['error'] = "Address/peer/user mismatch in outgoing envelope"
    
    return envelope

class CheckOutgoingHandler(Handler):
    """Handler for check outgoing."""

    @property
    def name(self) -> str:
        return "check_outgoing"

    def filter(self, envelope: dict[str, Any]) -> bool:
        """Check if this handler should process the envelope."""
        return filter_func(envelope)

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Process the envelope."""
        result = handler(envelope)
        if result:
            return [result]
        return []
