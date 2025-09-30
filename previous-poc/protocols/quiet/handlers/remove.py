"""
Remove handler - Checks if events should be removed based on removal rules.

From plan.md:
- Early phase: Has `event_id` AND `should_remove` is not false (before decryption)
- Content phase: Has `event_plaintext` AND `event_type` AND `should_remove` is not false (after decryption)
- Action: Checks deletion records and calls event type removers
- Output Type: Same envelope with `should_remove: false` OR drops envelope (returns None)
"""
from typing import Dict, List, Any, Optional, Set
import sqlite3
import importlib
from core.handlers import Handler


# Cache for loaded remover modules
_removers_cache: Dict[str, Any] = {}


def filter_func(envelope: dict[str, Any]) -> bool:
    """
    Process envelopes that might need removal.
    Two phases:
    1. Early check - has event_id (but maybe no plaintext yet)
    2. Content check - has plaintext and type
    """
    # Skip if already marked for keeping
    if envelope.get('should_remove') is False:
        return False
    
    # Phase 1: Has event_id (early check)
    has_event_id = 'event_id' in envelope
    
    # Phase 2: Has content (can do type-specific checks)
    has_content = 'event_plaintext' in envelope and 'event_type' in envelope
    
    return has_event_id or has_content


def handler(envelope: dict[str, Any], db: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """
    Check if event should be removed.
    
    Args:
        envelope: dict[str, Any] to check
        db: Database connection
        
    Returns:
        dict[str, Any] with should_remove: false if keeping, None if removing
    """
    # Phase 1: Check explicit deletions by event_id
    if envelope.get('event_id'):
        if is_explicitly_deleted(envelope['event_id'], db):
            return None  # Drop the envelope
    
    # Phase 2: Check type-specific removal rules (if we have content)
    if envelope.get('event_plaintext') and envelope.get('event_type'):
        event_type = envelope['event_type']
        remover = get_remover(event_type)
        
        if remover and hasattr(remover, 'should_remove'):
            try:
                removal_context = get_removal_context(db)
                if remover.should_remove(envelope, removal_context):
                    return None  # Drop the envelope
            except Exception as e:
                print(f"Remover error for {event_type}: {e}")
    
    # Event passes removal checks
    envelope['should_remove'] = False
    return envelope


def is_explicitly_deleted(event_id: str, db: sqlite3.Connection) -> bool:
    """Check if this event_id has been explicitly deleted."""
    cursor = db.execute("""
        SELECT 1 FROM deleted_events 
        WHERE event_id = ? 
        LIMIT 1
    """, (event_id,))
    
    return cursor.fetchone() is not None


def get_removal_context(db: sqlite3.Connection) -> Dict[str, Set[str]]:
    """Get context for removal decisions."""
    context: Dict[str, Set[str]] = {
        'deleted_channels': set(),
        'removed_users': set(),
        'deleted_messages': set()
    }
    
    # Get deleted channels
    cursor = db.execute("SELECT channel_id FROM deleted_channels")
    context['deleted_channels'] = {row['channel_id'] for row in cursor}
    
    # Get removed users  
    cursor = db.execute("SELECT user_id FROM removed_users")
    context['removed_users'] = {row['user_id'] for row in cursor}
    
    # Get deleted messages (include all explicit deletions regardless of reason)
    cursor = db.execute("""
        SELECT event_id FROM deleted_events
    """)
    context['deleted_messages'] = {row['event_id'] for row in cursor}
    
    return context


def get_remover(event_type: str) -> Optional[Any]:
    """Load and cache remover for an event type."""
    if event_type in _removers_cache:
        return _removers_cache[event_type]
    
    try:
        module = importlib.import_module(f'protocols.quiet.events.{event_type}.remover')
        _removers_cache[event_type] = module
        return module
    except ImportError:
        # Not all event types need removers
        _removers_cache[event_type] = None
        return None

class RemoveHandler(Handler):
    """Handler for remove."""

    @property
    def name(self) -> str:
        return "remove"

    def filter(self, envelope: dict[str, Any]) -> bool:
        """Check if this handler should process the envelope."""
        if not isinstance(envelope, dict):
            print(f"[remove] WARNING: filter got {type(envelope)} instead of dict: {envelope}")
            return False
        return filter_func(envelope)

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Process the envelope."""
        result = handler(envelope, db)
        if result:
            return [result]
        return []
