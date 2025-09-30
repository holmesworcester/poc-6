"""Job scheduler for running periodic jobs."""

import sqlite3
import time
from typing import Dict, List, Any, Optional


class JobScheduler:
    """Schedules and returns due jobs (operation executions)."""

    def __init__(self, db_path: str, job_configs: Optional[Dict[str, int]] = None, protocol_name: Optional[str] = None):
        """
        Initialize the job scheduler.

        Args:
            db_path: Path to the database
            job_configs: Optional dict of job_name -> frequency_ms mappings
        """
        self.db_path = db_path
        self.protocol_name = protocol_name or ''
        # Job configurations (name -> frequency_ms)
        self.job_configs = job_configs or self._load_jobs()

    def _load_jobs(self) -> Dict[str, int]:
        """Load job frequencies from project root jobs.py (JOBS list)."""
        try:
            import importlib
            module_name = f'protocols.{self.protocol_name}.jobs' if self.protocol_name else 'jobs'
            jobs_mod = importlib.import_module(module_name)
            jobs_list = getattr(jobs_mod, 'JOBS', [])
            # Return as mapping op->every_ms; duplicates last one wins
            return {job['op']: int(job.get('every_ms', 0) or 0) for job in jobs_list if 'op' in job}
        except Exception:
            return {}

    def tick(self) -> List[Dict[str, Any]]:
        """Check for due jobs and return list of due job dicts {op, params}."""
        due: List[Dict[str, Any]] = []
        time_now_ms = int(time.time() * 1000)

        # Open a fresh connection for this check
        db = sqlite3.connect(self.db_path)
        cursor = db.cursor()

        try:
            for op_name, frequency_ms in self.job_configs.items():
                if not frequency_ms or frequency_ms <= 0:
                    continue
                # Check when job last ran
                cursor.execute(
                    "SELECT last_run_ms FROM job_runs WHERE job_name = ?",
                    (op_name,),
                )
                row = cursor.fetchone()

                if row:
                    last_run_ms = row[0]
                    if time_now_ms - last_run_ms < frequency_ms:
                        continue  # Not due yet
                # else: Never run, so it's due

                due.append({'op': op_name, 'params': {}})
                # Update last_run_ms optimistically
                cursor.execute(
                    "INSERT OR REPLACE INTO job_runs (job_name, last_run_ms) VALUES (?, ?)",
                    (op_name, time_now_ms),
                )
                db.commit()
        finally:
            db.close()

        return due
