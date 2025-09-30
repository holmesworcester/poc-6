"""Job handler - executes scheduled jobs."""

import json
import sqlite3
import time
from typing import Dict, List, Any, Tuple, Callable
from core.handlers import Handler


class JobHandler(Handler):
    """Executes scheduled jobs that maintain state between runs."""

    @property
    def name(self) -> str:
        return "job"

    def __init__(self) -> None:
        super().__init__()
        self.jobs: Dict[str, Callable[[Dict[str, Any], sqlite3.Connection, int], Tuple[bool, Dict[str, Any], List[Dict[str, Any]]]]] = self._load_jobs()

    def _load_jobs(self) -> Dict[str, Callable[[Dict[str, Any], sqlite3.Connection, int], Tuple[bool, Dict[str, Any], List[Dict[str, Any]]]]]:
        """Dynamically load all job functions from event directories."""
        import os
        import importlib
        from pathlib import Path

        jobs: Dict[str, Callable[[Dict[str, Any], sqlite3.Connection, int], Tuple[bool, Dict[str, Any], List[Dict[str, Any]]]]] = {}

        # Find the events directory
        events_dir = Path(__file__).parent.parent / 'events'

        if not events_dir.exists():
            return jobs

        # Scan each event type directory for job.py
        for event_dir in events_dir.iterdir():
            if not event_dir.is_dir():
                continue

            job_file = event_dir / 'job.py'
            if not job_file.exists():
                continue

            # Import the job module
            event_type = event_dir.name
            module_name = f'protocols.quiet.events.{event_type}.job'

            try:
                module = importlib.import_module(module_name)

                # Look for a function named {event_type}_job
                job_function_name = f'{event_type}_job'
                if hasattr(module, job_function_name):
                    jobs[event_type] = getattr(module, job_function_name)
                    # print(f"[JobHandler] Loaded job: {event_type}")
            except Exception as e:
                print(f"[JobHandler] Failed to load job from {module_name}: {e}")

        return jobs

    def filter(self, envelope: Dict[str, Any]) -> bool:
        """Process run_job envelopes."""
        if envelope.get('event_type') == 'run_job':
            print(f"[JobHandler.filter] run_job event for job_name={envelope.get('job_name')}, available jobs={list(self.jobs.keys())}")
        return (envelope.get('event_type') == 'run_job' and
                envelope.get('job_name') in self.jobs)

    def process(self, envelope: Dict[str, Any], db: sqlite3.Connection) -> List[Dict[str, Any]]:
        """Execute the specified job."""
        job_name = envelope['job_name']
        job_fn = self.jobs[job_name]
        time_now_ms = int(time.time() * 1000)

        # Load state for this job
        cursor = db.cursor()
        cursor.execute("""
            SELECT state_json FROM job_states
            WHERE job_name = ?
        """, (job_name,))
        row = cursor.fetchone()
        state = json.loads(row[0]) if row else {}

        # Run the job (jobs get read-only access)
        try:
            success, new_state, envelopes = job_fn(state, db, time_now_ms)
        except Exception as e:
            print(f"[JobHandler] Job {job_name} failed: {e}")
            # Track failure
            cursor.execute("""
                INSERT OR REPLACE INTO job_runs (job_name, last_run_ms, last_failure_ms, failure_count, last_state)
                VALUES (?, ?, ?, COALESCE((SELECT failure_count FROM job_runs WHERE job_name = ?), 0) + 1, ?)
            """, (job_name, time_now_ms, time_now_ms, job_name, json.dumps(state)))
            db.commit()
            return []

        if success:
            # Save new state
            cursor.execute("""
                INSERT OR REPLACE INTO job_states (job_name, state_json, updated_ms)
                VALUES (?, ?, ?)
            """, (job_name, json.dumps(new_state), time_now_ms))

            # Track success
            cursor.execute("""
                INSERT OR REPLACE INTO job_runs (
                    job_name, last_run_ms, last_success_ms, success_count, failure_count, last_state
                )
                VALUES (
                    ?, ?, ?,
                    COALESCE((SELECT success_count FROM job_runs WHERE job_name = ?), 0) + 1,
                    COALESCE((SELECT failure_count FROM job_runs WHERE job_name = ?), 0),
                    ?
                )
            """, (job_name, time_now_ms, time_now_ms, job_name, job_name, json.dumps(new_state)))

            db.commit()

            print(f"[JobHandler] Job {job_name} succeeded, emitting {len(envelopes)} envelopes")
            return envelopes
        else:
            # Track failure but don't update state
            cursor.execute("""
                INSERT OR REPLACE INTO job_runs (
                    job_name, last_run_ms, last_failure_ms, failure_count, success_count, last_state
                )
                VALUES (
                    ?, ?, ?,
                    COALESCE((SELECT failure_count FROM job_runs WHERE job_name = ?), 0) + 1,
                    COALESCE((SELECT success_count FROM job_runs WHERE job_name = ?), 0),
                    ?
                )
            """, (job_name, time_now_ms, time_now_ms, job_name, job_name, json.dumps(state)))
            db.commit()

            print(f"[JobHandler] Job {job_name} returned failure")
            return []
