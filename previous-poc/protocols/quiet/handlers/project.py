"""
Handler that projects validated events to state.
"""
import json
import time
from typing import List, Dict, Any, Optional
import sqlite3
import importlib
from core.handlers import Handler
from protocols.quiet.protocol_types import validate_envelope_fields, cast_envelope


class ProjectHandler(Handler):
    """
    Projects validated events using their type-specific projectors.
    Consumes: envelopes with validated=True
    Emits: envelopes with projected=True and deltas
    """
    
    def __init__(self) -> None:
        # Map of event types to their projector modules
        self.projectors: Dict[str, Any] = {}
        self._load_projectors()
    
    @property
    def name(self) -> str:
        return "project"
    
    def filter(self, envelope: dict[str, Any]) -> bool:
        """Process validated events that haven't been projected."""
        if not isinstance(envelope, dict):
            print(f"[project] WARNING: filter got {type(envelope)} instead of dict")
            return False
        return (
            envelope.get('validated') is True and
            envelope.get('projected') is not True and
            'event_id' in envelope  # Need event_id for projection
        )
    
    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Project event to state."""
        
        # Get event type
        event_type = envelope.get('event_type')
        if not event_type:
            envelope['error'] = "No event_type for projection"
            return []
        projector = self.projectors.get(event_type)
        
        if not projector:
            envelope['error'] = f"No projector for event type: {event_type}"
            # Don't re-emit even on missing projector
            return []
        
        try:
            # Handle local metadata for self-created local peers (peer_secret)
            if envelope.get('self_created') and 'local_metadata' in envelope:
                self._store_local_metadata(envelope, db)

            # Run projector - it should emit deltas
            deltas = projector.project(envelope)
            print(f"[project] Generated {len(deltas) if deltas else 0} deltas")
            
            # Apply deltas
            from core.deltas import DeltaApplicator
            if deltas:
                for delta in deltas:
                    print(f"[project] Applying delta: {delta}")
                    DeltaApplicator.apply(delta, db)

            # No metadata projection for ES; sync selection will be peer-scoped or hint-based
            
            # Mark as projected and include deltas
            envelope['projected'] = True
            envelope['deltas'] = deltas
            
            # If this event unblocks others, emit unblock events
            try:
                event_id = envelope.get('event_id')
                if event_id is not None:
                    unblock_envelopes = self._check_unblocks(event_id, db)
                else:
                    unblock_envelopes = []
            except sqlite3.OperationalError:
                # Table doesn't exist yet
                unblock_envelopes = []
            
            db.commit()
            
            # Don't re-emit the projected envelope - only emit unblocked events
            return unblock_envelopes
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            db.rollback()
            envelope['error'] = f"Projection failed: {str(e)}"
            envelope['projected'] = True  # Mark as projected even on error to prevent loops
            # Don't re-emit on error either
            return []
    
    
    def _store_local_metadata(self, envelope: dict[str, Any], db: sqlite3.Connection) -> None:
        """Store local metadata for self-created identities."""
        # Check both local_metadata and secret fields
        local_metadata = envelope.get('local_metadata', {})
        secret = envelope.get('secret', {})
        event_data = envelope.get('event_plaintext', {})

        # For peer_secret events, the private key is already stored by the projector
        # No need for additional storage in core_identities
    
    def _check_unblocks(self, event_id: str, db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Check if this event unblocks any waiting events."""
        cursor = db.execute("""
            SELECT DISTINCT blocked_event_id 
            FROM blocked_by 
            WHERE blocking_event_id = ?
        """, (event_id,))
        
        unblock_envelopes = []
        for row in cursor:
            # Create minimal envelope to trigger re-processing
            unblock_env = {
                'event_id': row['blocked_event_id'],
                'unblocked': True
            }
            unblock_envelopes.append(unblock_env)
        
        return unblock_envelopes
    
    def _load_projectors(self) -> None:
        """Dynamically load all event type projectors from the events directory."""
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
                projector_file = event_dir / 'projector.py'

                # Check if projector.py exists
                if projector_file.exists():
                    try:
                        module = importlib.import_module(f'protocols.quiet.events.{event_type}.projector')
                        self.projectors[event_type] = module
                        print(f"Loaded projector for {event_type}")
                    except ImportError as e:
                        print(f"Failed to load {event_type} projector: {e}")
                    except Exception as e:
                        print(f"Error loading {event_type} projector: {e}")
