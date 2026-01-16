"""PSK (Pre-Shared Key) authentication for the API.

The API generates a random token on startup and writes it to a known location.
The frontend reads this token and includes it in every request.

Security model:
- API only binds to localhost (127.0.0.1 or Unix socket)
- Token file has restrictive permissions (0600)
- Token rotates on each startup
- Constant-time comparison prevents timing attacks
"""

import base64
import os
import secrets
from pathlib import Path
from typing import Callable

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core import crypto


# Token storage
_api_token: bytes | None = None
_auth_enabled: bool = True


def generate_token() -> bytes:
    """Generate a new random 32-byte token using existing crypto."""
    return crypto.generate_secret()


def get_token_path(peer_id: str) -> Path:
    """Get the path where the API token should be stored."""
    quiet_dir = Path.home() / ".quiet" / peer_id
    return quiet_dir / "api_token"


def init_auth(peer_id: str | None = None, token_dir: Path | None = None) -> bytes:
    """Initialize authentication by generating and storing a token.

    Args:
        peer_id: The peer ID for this API instance. If None, token is
                 generated but not written to disk.
        token_dir: Override directory for token file (for testing).
                   If None, uses ~/.quiet/{peer_id}/

    Returns:
        The generated token bytes.
    """
    global _api_token

    _api_token = generate_token()

    if peer_id:
        if token_dir:
            token_path = token_dir / "api_token"
        else:
            token_path = get_token_path(peer_id)

        token_path.parent.mkdir(parents=True, exist_ok=True)

        # Write with restrictive permissions
        token_path.write_bytes(_api_token)
        os.chmod(token_path, 0o600)

        print(f"API token written to: {token_path}")

    return _api_token


def read_token_from_file(peer_id: str, token_dir: Path | None = None) -> bytes | None:
    """Read the API token from the token file.

    This is used by frontend clients to get the token for authentication.

    Args:
        peer_id: The peer ID to read token for.
        token_dir: Override directory (for testing).

    Returns:
        The token bytes, or None if file doesn't exist.
    """
    if token_dir:
        token_path = token_dir / "api_token"
    else:
        token_path = get_token_path(peer_id)

    if not token_path.exists():
        return None

    return token_path.read_bytes()


def get_token() -> bytes | None:
    """Get the current API token."""
    return _api_token


def set_token(token: bytes) -> None:
    """Set the API token directly (for testing)."""
    global _api_token
    _api_token = token


def disable_auth() -> None:
    """Disable authentication (for testing)."""
    global _auth_enabled
    _auth_enabled = False


def enable_auth() -> None:
    """Enable authentication."""
    global _auth_enabled
    _auth_enabled = True


def is_auth_enabled() -> bool:
    """Check if authentication is enabled."""
    return _auth_enabled


def validate_token(provided_token: bytes) -> bool:
    """Validate a token against the stored token.

    Uses constant-time comparison to prevent timing attacks.
    """
    if _api_token is None:
        return False
    return secrets.compare_digest(provided_token, _api_token)


def extract_token_from_header(auth_header: str) -> bytes | None:
    """Extract and decode token from Authorization header.

    Expected format: "Bearer <base64_token>"
    """
    if not auth_header.startswith("Bearer "):
        return None

    token_b64 = auth_header[7:]  # Skip "Bearer "
    try:
        return base64.b64decode(token_b64)
    except Exception:
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that validates PSK token on every request."""

    # Paths that don't require authentication
    PUBLIC_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next: Callable):
        # Skip auth if disabled (testing)
        if not _auth_enabled:
            return await call_next(request)

        # Skip auth for public paths
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        # Skip auth for OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        # Get Authorization header
        auth_header = request.headers.get("Authorization", "")

        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Authorization header"},
            )

        # Extract and validate token
        token = extract_token_from_header(auth_header)

        if token is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid Authorization header format. Expected: Bearer <base64_token>"},
            )

        if not validate_token(token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API token"},
            )

        # Token valid, proceed
        return await call_next(request)


def get_auth_header(token: bytes | None = None) -> dict[str, str]:
    """Get the Authorization header dict for making authenticated requests.

    Useful for clients and tests.
    """
    if token is None:
        token = _api_token
    if token is None:
        raise ValueError("No token available")

    token_b64 = base64.b64encode(token).decode("ascii")
    return {"Authorization": f"Bearer {token_b64}"}
