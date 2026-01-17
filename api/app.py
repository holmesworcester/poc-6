"""
Quiet Protocol HTTP API

FastAPI server that wraps the event module functions.
Runs on Unix socket to avoid port conflicts.

Usage:
    python -m api --peer-id PEER123                    # Default socket
    python -m api --peer-id PEER123 --socket /tmp/x.sock  # Custom socket
    python -m api --peer-id PEER123 --port 8080        # TCP port

On startup, generates a PSK token and writes it to ~/.quiet/{peer_id}/api_token
Frontend reads this token and includes it in Authorization header.
"""

import argparse
import os
import socket as sock_module
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.core.database import get_db, init_db, set_peer_id
from api.core.auth import AuthMiddleware, init_auth, disable_auth
from api.routes import channels, messages, networks, users, files, sync

DEFAULT_SOCKET = "/tmp/quiet-api.sock"

# Global peer_id set at startup (used by lifespan)
_startup_peer_id: str | None = None


def cleanup_stale_socket(socket_path: str) -> None:
    """Remove socket file if no server is listening."""
    sock_path = Path(socket_path)
    if not sock_path.exists():
        return

    # Try connecting to see if a server is running
    try:
        s = sock_module.socket(sock_module.AF_UNIX, sock_module.SOCK_STREAM)
        s.settimeout(1)
        s.connect(socket_path)
        s.close()
        # Server is running - can't start another
        raise SystemExit(f"Error: API already running on {socket_path}")
    except (ConnectionRefusedError, sock_module.timeout):
        # Stale socket from crashed process - safe to remove
        sock_path.unlink()
        print(f"Cleaned up stale socket: {socket_path}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    print("Starting Quiet API...")
    init_db()

    # Initialize auth with peer_id if provided
    if _startup_peer_id:
        set_peer_id(_startup_peer_id)
        init_auth(_startup_peer_id)
        print(f"Auth initialized for peer: {_startup_peer_id}")
    else:
        print("Warning: No peer_id provided, auth token not written to disk")

    yield
    # Shutdown
    print("Shutting down Quiet API...")


app = FastAPI(
    title="Quiet Protocol API",
    description="HTTP API for the Quiet Protocol",
    version="0.1.0",
    lifespan=lifespan,
)

# PSK authentication middleware
app.add_middleware(AuthMiddleware)

# CORS for development (Vite proxy handles this, but just in case)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include routers
app.include_router(networks.router, prefix="/api", tags=["networks"])
app.include_router(channels.router, prefix="/api", tags=["channels"])
app.include_router(messages.router, prefix="/api", tags=["messages"])
app.include_router(users.router, prefix="/api", tags=["users"])
app.include_router(files.router, prefix="/api", tags=["files"])
app.include_router(sync.router, prefix="/api", tags=["sync"])


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler."""
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "type": type(exc).__name__},
    )


def main():
    """CLI entry point."""
    global _startup_peer_id

    parser = argparse.ArgumentParser(description="Quiet Protocol API Server")
    parser.add_argument(
        "--peer-id",
        required=True,
        help="Peer ID this API instance serves (required)",
    )
    parser.add_argument(
        "--socket",
        default=DEFAULT_SOCKET,
        help=f"Unix socket path (default: {DEFAULT_SOCKET})",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="TCP port (overrides --socket)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for TCP mode (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable authentication (for development)",
    )
    args = parser.parse_args()

    # Set peer_id for lifespan to use
    _startup_peer_id = args.peer_id

    # Disable auth if requested
    if args.no_auth:
        disable_auth()
        print("Warning: Authentication disabled")

    import uvicorn

    if args.port:
        # TCP mode
        print(f"Starting API on http://{args.host}:{args.port}")
        uvicorn.run(
            "api.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
    else:
        # Unix socket mode
        cleanup_stale_socket(args.socket)
        print(f"Starting API on socket: {args.socket}")
        uvicorn.run(
            "api.app:app",
            uds=args.socket,
            reload=args.reload,
        )


if __name__ == "__main__":
    main()
