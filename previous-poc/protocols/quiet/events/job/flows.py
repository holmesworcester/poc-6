"""Flows for job operations."""

from typing import Dict, Any
from core.flows import FlowCtx, flow_op


@flow_op()  # Registers as 'job.run'
def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run a specific job."""
    ctx = FlowCtx.from_params(params)
    job_name = params.get('job_name', '')

    if not job_name:
        raise ValueError('job_name is required')

    # Emit run_job event to trigger the job handler
    ctx.runner.run(protocol_dir=ctx.protocol_dir, input_envelopes=[{
        'event_type': 'run_job',
        'job_name': job_name,
    }], db=ctx.db)

    return {'ids': {}, 'data': {'job_triggered': job_name}}