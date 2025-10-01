"""Key management functions."""
from typing import Any
from crypto import crypto

def parse_hint(blob: bytes) -> Any:
    """Parse a key hint from a blob."""
    # TODO: for whatever the key hint length is (16 bytes?) extract that from the start of the blob
    hint = blob[:16]
    return hint

def get_key(hint: Any, db: Any) -> Any:
    """Get a key from the database using a hint."""
    key = db.query("SELECT key FROM keys WHERE hint = ?", (hint,))
    return key

def create_sym_key(db: Any) -> Any:
    """Create a symmetric key and store it in the database."""
    key = crypto.generate_symmetric_key()
    db.execute("INSERT INTO keys (key, type) VALUES (?, ?)", (key, "symmetric"))
    return {key: key, "type": "symmetric", "hint": crypto.hash(key)}