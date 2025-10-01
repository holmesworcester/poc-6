"""Incoming event management."""
from typing import Any


def drain(batch_size: int) -> list[bytes]:
    """Drain (select and delete) incoming transit blobs up to batch_size."""
    blobs = db.query("SELECT blob FROM incoming_blobs LIMIT ?", (batch_size,))
    db.execute("DELETE FROM incoming_blobs WHERE id IN (SELECT id FROM incoming_blobs LIMIT ?)", (batch_size,)) # This assumes it hasn't changed in the meantime; a more robust approach would use a transaction or return IDs from the first query
    return blobs


def create(blob: bytes, db: Any, t_ms: int) -> None:
    """Create an incoming transit blob entry at t_ms"""
    sent_at = t_ms
    db.execute("INSERT INTO incoming_blobs (blob, sent_at) VALUES (?, ?)", (blob, sent_at))
    return None
