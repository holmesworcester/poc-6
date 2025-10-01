"""Key management functions."""
from typing import Any
import crypto

def extract_hint(blob: bytes) -> bytes:
    """Extract the key hint from the start of a blob."""
    hint = blob[:crypto.HINT_SIZE]
    return hint


def get_key(hint: bytes, db: Any) -> Any:
    """Get a key from the database using a hint."""
    result = db.query_one("SELECT key FROM keys WHERE hint = ?", (hint,))
    return result['key'] if result else None

def create_sym_key(db: Any) -> dict[str, Any]:
    """Create a symmetric key and store it in the database."""
    key = crypto.generate_secret()
    hint = crypto.hash(key)
    db.execute("INSERT INTO keys (key, type, hint) VALUES (?, ?, ?)", (key, "symmetric", hint))
    return {"key": key, "type": "symmetric", "hint": hint}