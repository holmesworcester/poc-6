"""
Event handling with transaction support.

Key changes:
- Each event is processed in its own transaction by default
- Failed projections automatically rollback
- Batch processing available for tick operations
- 'blocked' list replaced with logging
"""
import importlib.util
import os
import logging
from core.handler_discovery import build_handler_map, load_handler_config
from core.schema_validator import validate_against_schema

# Set up logging
logger = logging.getLogger(__name__)


def handle(db, envelope, time_now_ms, auto_transaction=True):
    """
    Route envelopes to appropriate handlers based on type or error state.
    
    Args:
        db: Database instance
        envelope: Event envelope to process
        time_now_ms: Current timestamp in milliseconds
        auto_transaction: If True, wrap processing in a transaction
    """
    if auto_transaction and hasattr(db, 'begin_transaction'):
        db.begin_transaction()
    
    try:
        # Check for error in metadata (missing key scenario)
        if 'error' in envelope.get('metadata', {}):
            event_type = 'missing_key'
        else:
            # Get payload - this is the only supported format
            payload = envelope.get('payload', {})
            # Get event type from payload
            event_type = payload.get('type')
        
        # Log what we're handling
        if os.environ.get("TEST_MODE"):
            print(f"[handle] Processing event type: {event_type}")
        
        # Get handler base path (for tests vs production)
        handler_base = os.environ.get("HANDLER_PATH", "handlers")
        
        # Build handler map dynamically from available handlers
        handler_map = build_handler_map(handler_base)
        
        # If no event type, check if unknown handler exists; if not, drop the event
        if not event_type:
            handler_name = handler_map.get('unknown')
            if not handler_name:
                # Log error and rollback
                logger.error(f"No handler for event with no type: {envelope}")
                if auto_transaction and hasattr(db, 'rollback'):
                    db.rollback()
                return db
            event_type = 'unknown'
        else:
            handler_name = handler_map.get(event_type)
        
        # Log handler mapping
        if os.environ.get("TEST_MODE"):
            print(f"[handle] Handler map: {handler_map}")
            print(f"[handle] Selected handler: {handler_name} for type: {event_type}")
        
        if not handler_name:
            # Route to unknown handler for unrecognized types
            handler_name = handler_map.get('unknown')
            if not handler_name:
                # Log error and rollback
                logger.error(f"No handler for event type '{event_type}': {envelope}")
                if auto_transaction and hasattr(db, 'rollback'):
                    db.rollback()
                return db
        
        # Load handler config
        handler_dir = f"{handler_base}/{handler_name}"
        config = load_handler_config(handler_name, handler_base)
        
        if not config:
            logger.error(f'Handler config not found for: {handler_name}')
            if auto_transaction:
                db.rollback()
            return db
        
        # Load and run projector
        projector_path = f"{handler_dir}/projector.py"
        if os.path.exists(projector_path):
            spec = importlib.util.spec_from_file_location("projector", projector_path)
            projector_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(projector_module)
            
            # Validate against handler schema when available (payload only)
            try:
                schema = config.get('schema') if isinstance(config, dict) else None
                if schema:
                    payload = envelope.get('payload', {})
                    ok, err = validate_against_schema(payload, schema)
                    if not ok:
                        raise ValueError(f"Payload validation failed for type '{event_type}': {err}")
            except Exception as ve:
                # Rollback and re-raise to caller
                if auto_transaction and hasattr(db, 'rollback'):
                    db.rollback()
                raise
            
            # Run projector with full envelope
            result = projector_module.project(db, envelope, time_now_ms)
            if result is not None:
                db = result
            
            # Commit if transaction successful
            if auto_transaction and hasattr(db, 'commit'):
                db.commit()
            
        else:
            logger.error(f'Projector not found for handler: {handler_name}')
            if auto_transaction and hasattr(db, 'rollback'):
                db.rollback()
            
    except Exception as e:
        # Rollback on any error
        if auto_transaction and hasattr(db, 'rollback'):
            db.rollback()
        
        # Log error
        logger.error(f"Failed to process event: {str(e)}", exc_info=True)
        
        raise  # Re-raise for caller to handle
    
    return db


def handle_batch(db, envelopes, time_now_ms):
    """
    Process multiple events, each in its own transaction.
    
    Args:
        db: Database instance
        envelopes: List of event envelopes to process
        time_now_ms: Current timestamp in milliseconds
        
    Returns:
        Tuple of (db, successful_count, failed_count)
    """
    successful = 0
    failed = 0
    
    for envelope in envelopes:
        try:
            db = handle(db, envelope, time_now_ms, auto_transaction=True)
            successful += 1
        except Exception as e:
            failed += 1
            # Individual failure doesn't stop batch processing
            logger.warning(f"Failed to process event in batch: {e}")
    
    return db, successful, failed
