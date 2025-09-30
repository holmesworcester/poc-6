"""
Handler that validates events using event type validators.

Notes:
- Pure validator: no DB reads/writes, no indexing, no unblocking.
- Emits `validated: true` and sets `deps_included_and_valid = False` to allow
  resolve_deps to index the `event_id` into `validated_events` and unblock
  dependents.
"""
import json
from typing import List, Dict, Any, cast as type_cast
import sqlite3
import importlib
from core.handlers import Handler
from protocols.quiet.protocol_types import validate_envelope_fields
from protocols.quiet.protocol_types import ValidatableEvent, BaseEnvelope
from protocols.quiet.handlers.event_store import purge_event


class ValidateHandler(Handler):
    """
    Validates events using their type-specific validators.
    Consumes: envelopes with event_plaintext, sig_checked=True
    Emits: envelopes with validated=True
    """
    
    def __init__(self) -> None:
        # Map of event types to their validator modules
        self.validators: Dict[str, Any] = {}
        self._load_validators()
    
    @property
    def name(self) -> str:
        return "validate"
    
    def filter(self, envelope: dict[str, Any]) -> bool:
        """Process envelopes ready for validation."""
        # Skip if already has error
        if envelope.get('error'):
            return False

        # For self-created events, they get validated after signing but before encryption (no event_id yet)
        # For received events, they get validated after decryption (event_id exists)
        # Allow local-only secret events to bypass signature ONLY if created locally.
        etype = envelope.get('event_plaintext', {}).get('type')
        is_local_only_secret = etype in ('peer_secret', 'key_secret', 'prekey_secret')
        allow_local_secret = is_local_only_secret and (
            envelope.get('self_created') is True or envelope.get('local_only') is True
        )
        sig_ok = envelope.get('sig_checked') is True or allow_local_secret

        # Debug network and group events
        if etype in ('network', 'group'):
            has_fields = validate_envelope_fields(envelope, {'event_plaintext', 'event_type'})
            result = (
                has_fields and
                sig_ok and
                not envelope.get('validated', False) and
                (envelope.get('self_created') or 'event_id' in envelope)
            )
            print(f"[validate filter] {etype}: has_fields={has_fields}, sig_ok={sig_ok}, validated={envelope.get('validated')}, self_created={envelope.get('self_created')}, event_id={'event_id' in envelope}, WILL PROCESS={result}")

        return (
            validate_envelope_fields(envelope, {'event_plaintext', 'event_type'}) and
            sig_ok and
            not envelope.get('validated', False) and
            (envelope.get('self_created') or 'event_id' in envelope)
        )
    
    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Validate event using type-specific validator (pure).

        Side effects like indexing validated IDs and unblocking dependents are
        owned by resolve_deps; validator only annotates the envelope.
        """
        
        # We don't need full DecryptedEnvelope validation since event_id comes later
        if not envelope.get('event_plaintext'):
            envelope['error'] = "No event_plaintext to validate"
            return []
            
        event_type = envelope['event_plaintext'].get('type')
        if not event_type:
            envelope['error'] = "No event type specified"
            return []
        
        # Get validator for this type
        validator = self.validators.get(event_type)
        if not validator:
            envelope['error'] = f"No validator for event type: {event_type}"
            return []
        
        # Run validation - validator expects the full envelope
        try:
            is_valid = validator.validate(envelope)
            if is_valid:
                envelope['validated'] = True
                envelope['event_type'] = event_type
                # Preserve deps_included_and_valid if it was already true
                # This ensures the crypto handler can encrypt the validated event
                if envelope.get('deps_included_and_valid') is not True:
                    envelope['deps_included_and_valid'] = False  # Let resolve_deps handle it
                # Emit only the validated envelope; resolve_deps listens and acts
                return [envelope]
            else:
                envelope['error'] = "Validation failed"
                return []  # Drop the envelope
        except Exception as e:
            envelope['error'] = f"Validation error: {str(e)}"
            return []  # Drop the envelope

        return [envelope]
    
    def _load_validators(self) -> None:
        """Dynamically load all event type validators from the events directory."""
        import os
        from pathlib import Path

        # Find the events directory
        events_dir = Path(__file__).parent.parent / 'events'

        if not events_dir.exists():
            print(f"Events directory not found: {events_dir}")
            return

        # Iterate through all subdirectories in events/
        for event_dir in events_dir.iterdir():
            if event_dir.is_dir() and not event_dir.name.startswith('_'):
                event_type = event_dir.name
                validator_file = event_dir / 'validator.py'

                # Check if validator.py exists
                if validator_file.exists():
                    try:
                        module = importlib.import_module(f'protocols.quiet.events.{event_type}.validator')
                        self.validators[event_type] = module
                        print(f"Loaded validator for {event_type}")
                    except ImportError as e:
                        print(f"Failed to load {event_type} validator: {e}")
                    except Exception as e:
                        print(f"Error loading {event_type} validator: {e}")
