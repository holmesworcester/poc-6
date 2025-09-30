"""
Tick processing.

Purposefully minimal: discover handlers with jobs and run them in order.
Jobs run independently; failures are logged but do not stop the tick.
"""
import os
import logging
from core.command import run_command
from core.handle import handle_batch

logger = logging.getLogger(__name__)
# In test mode, surface debug logs to aid diagnosis
if os.environ.get("TEST_MODE"):
    try:
        logger.setLevel(logging.DEBUG)
    except Exception:
        pass


def tick(db, time_now_ms=None):
    """Run all configured jobs once and return the updated `db`."""
    return run_all_jobs(db, time_now_ms)

def run_all_jobs(db, time_now_ms):
    """Discover and execute each handler's job command if declared."""
    from core.handler_discovery import discover_handlers, load_handler_config

    handler_base = os.environ.get("HANDLER_PATH", "handlers")
    handler_names = discover_handlers(handler_base)

    for handler_name in handler_names:
        config = load_handler_config(handler_name, handler_base)
        if not config:
            continue

        job_command = config.get('job')
        commands = (config.get('commands') or {})
        if not job_command or job_command not in commands:
            continue

        try:
            input_data = {}
            if time_now_ms is not None:
                input_data["time_now_ms"] = time_now_ms
            logger.debug(f"[tick] Running job {handler_name}.{job_command}")
            db, _ = run_command(handler_name, job_command, input_data, db, time_now_ms)
        except Exception as e:
            logger.error(f"Job {handler_name}.{job_command} failed: {e}")
            continue

    return db
