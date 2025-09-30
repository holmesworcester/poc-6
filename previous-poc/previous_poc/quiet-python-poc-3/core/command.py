"""
Command execution and event projection.

Executes a handler command, applies any direct infrastructure updates, and
projects returned events through the framework. If the database supports
transactions, the entire operation is atomic.
"""
import importlib.util
import os
import logging
import time
import uuid
from core.handler_discovery import get_handler_path
from core.handle import handle
from core.types import validate_event

# Set up logging
logger = logging.getLogger(__name__)

def is_infrastructure_update(key, value, db):
    """Return True if a direct update targets infra state only.

    For performance and clarity, direct dict updates are no longer allowed.
    Infrastructure queues should be SQL tables. This function always returns
    False to prevent silent dict writes from commands.
    """
    return False


def run_command(handler_name, command_name, input_data, db=None, time_now_ms=None):
    """
    Execute a command and project any returned events.
    Returns the modified db and command result.
    
    This is a first-class operation used by:
    - API endpoints to execute user commands
    - Tick to run periodic jobs
    - Tests to execute test scenarios
    """
    # If db supports retry, use it
    if hasattr(db, 'with_retry'):
        return db.with_retry(lambda: _run_command_with_tx(
            handler_name, command_name, input_data, db, time_now_ms
        ))
    else:
        return _run_command_with_tx(handler_name, command_name, input_data, db, time_now_ms)


def _run_command_with_tx(handler_name, command_name, input_data, db, time_now_ms):
    """Internal function that runs command with transaction"""
    handler_base = os.environ.get("HANDLER_PATH", "handlers")
    
    module_path = get_handler_path(handler_name, command_name, handler_base)
    if not module_path:
        raise ValueError(f"Command not found: {handler_name}/{command_name}")
    
    # Load command module
    spec = importlib.util.spec_from_file_location(command_name, module_path)
    command_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command_module)
    
    # Allow commands to manage their own transactions when needed
    manage_tx = bool(getattr(command_module, 'MANAGE_TRANSACTIONS', False))
    has_transactions = hasattr(db, 'begin_transaction')
    if has_transactions and not manage_tx:
        db.begin_transaction()
    
    try:
        # Execute command
        try:
            result = command_module.execute(input_data, db)
        except Exception as e:
            import traceback
            error_msg = f"Error in {handler_name}.{command_name}: {str(e)}"
            if os.environ.get("TEST_MODE"):
                print(f"[command] {error_msg}")
                print(f"[command] Traceback: {traceback.format_exc()}")
            raise Exception(error_msg) from e
        
        # Disallow 'db' in command returns entirely to enforce SQL-first semantics
        if not manage_tx and isinstance(result, dict) and 'db' in result:
            raise ValueError(
                f"Command '{handler_name}.{command_name}' returned a 'db' field. "
                f"Commands must modify state via SQL tables and events, and should not return 'db'."
            )
        
        # Project any new events/envelopes returned by the command
        if not manage_tx and isinstance(result, dict) and ('newEnvelopes' in result or 'newEvents' in result):
            items = result.get('newEnvelopes') or result.get('newEvents') or []
            for item in items:
                # If item looks like a new-model event, validate it
                if isinstance(item, dict) and 'type' in item and 'id' in item:
                    try:
                        validate_event(item, handler_base=handler_base)
                    except Exception as ve:
                        raise
                # Accept either full envelopes ({payload, metadata}) or raw event dicts
                if isinstance(item, dict) and 'payload' in item:
                    envelope = item  # assume already-formed envelope
                else:
                    envelope = {'payload': item, 'metadata': {}}
                # Project within the same transaction when supported
                db = handle(db, envelope, time_now_ms, auto_transaction=False)
        
        # Commit when successful
        if has_transactions and not manage_tx:
            db.commit()
            
    except Exception as e:
        if has_transactions and not manage_tx:
            db.rollback()
        raise
    
    return db, result
