"""Batch projection engine scaffolding for projection v2."""
from __future__ import annotations

from typing import Any


def project_batch(recorded_ids: list[str], db: Any) -> list[str | None]:
    """Project a batch of recorded_ids using v2 pipeline.

    Implementation is provided by v2 core workstream.
    """
    raise NotImplementedError
