# API Implementation Plan for Quiet Protocol

## Context

Building a RESTful API and isomorphic React Native prototype.

**What this is:**
- An easy-to-develop, easy-to-test prototype of the mobile app
- Same React Native code runs in browser (dev) and on device (prod)
- Browser-first development: hot reload, Chrome DevTools, Cypress
- API design validated before tackling native mobile complexity

**Key requirements:**
- Polling-first (100ms intervals when foreground)
- Lazy loading via FlatList/FlashList (works isomorphically)
- Cypress-testable via RN Web
- Python HTTP API maps directly to event module functions

## Phased Approach

### Phase 1 (Current): RN Web + Python HTTP API

Focus on web-first development. No native mobile complexity yet.

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (dev) / Electron (prod)                            │
│                                                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐ │
│  │   Cypress   │───▶│  RN Web     │───▶│  Python Backend │ │
│  │   Tests     │    │  (browser)  │    │  (HTTP/socket)  │ │
│  └─────────────┘    └─────────────┘    └─────────────────┘ │
│                            │                    │           │
│                            ▼                    ▼           │
│                     ┌─────────────┐    ┌─────────────────┐ │
│                     │   sql.js    │    │     SQLite      │ │
│                     │   (reads)   │    │     (writes)    │ │
│                     └─────────────┘    └─────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**Deliverables:**
- Full UI in React Native Web
- Python HTTP API (maps to event modules)
- Cypress component + E2E tests
- Works in browser and Electron

### Phase 2 (Future): Native Mobile

Requires solving:
- Python on mobile (hard) OR Rust core (big project)
- iOS NSE for push notification decryption
- Background sync on Android

**Options to evaluate after Phase 1:**
1. Rust core with uniffi bindings (Signal's approach)
2. TypeScript port + native crypto shim for NSE
3. Server-assisted mode (simpler but less local-first)

Phase 1 gives us a working product and validated API design before tackling native complexity.

## Local Dev: Zero Port Management

Unix sockets eliminate "address already in use" errors and port conflicts entirely.

```
┌─────────────────────────────────────────────────────────────┐
│ Browser (RN Web / Cypress)                                  │
│                                                             │
│   fetch('/api/messages')   ← relative URL, no port          │
│         │                                                   │
│         ▼ (HTTP)                                            │
└─────────┼───────────────────────────────────────────────────┘
          │
┌─────────▼───────────────────────────────────────────────────┐
│ Vite Dev Server (:5173)                    ← only port      │
│                                                             │
│   /api/* → proxy to Unix socket                             │
│   /*     → serve RN Web bundle                              │
│         │                                                   │
│         ▼ (Unix socket)                                     │
└─────────┼───────────────────────────────────────────────────┘
          │
┌─────────▼───────────────────────────────────────────────────┐
│ Python Backend                             ← no port        │
│                                                             │
│   listening on /tmp/quiet-api.sock                          │
└─────────────────────────────────────────────────────────────┘
```

### Single Dev Command

```json
// package.json
{
  "scripts": {
    "dev": "concurrently \"npm run dev:backend\" \"npm run dev:frontend\"",
    "dev:backend": "python -m api --socket /tmp/quiet-api.sock",
    "dev:frontend": "vite"
  }
}
```

```bash
# Just run this, every time, no thinking
npm run dev

# Handles:
# ✓ Cleans up stale sockets automatically
# ✓ Starts backend + frontend together
# ✓ No port selection needed
# ✓ No "address in use" errors
# ✓ Ctrl+C kills both
```

### Stale Socket Cleanup

```python
# api/app.py
import os
import socket
from pathlib import Path

SOCKET_PATH = "/tmp/quiet-api.sock"

def cleanup_stale_socket():
    """Remove socket file if no server is listening"""
    sock_path = Path(SOCKET_PATH)
    if not sock_path.exists():
        return

    # Try connecting to see if a server is running
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCKET_PATH)
        s.close()
        # Server is running - bail out
        raise SystemExit("Error: API already running on socket")
    except ConnectionRefusedError:
        # Stale socket from crashed process - safe to remove
        sock_path.unlink()
        print(f"Cleaned up stale socket: {SOCKET_PATH}")

@app.on_event("startup")
async def startup():
    cleanup_stale_socket()
```

### Vite Config

```typescript
// vite.config.ts
export default defineConfig({
  server: {
    proxy: {
      '/api': {
        target: { socketPath: '/tmp/quiet-api.sock' },
      }
    }
  }
});
```

### Benefits

- **No port conflicts** - Unix socket, not TCP port
- **No stale processes** - Auto-cleanup on startup
- **No configuration** - Just `npm run dev`
- **Cypress just works** - Same relative URLs
- **Frontend code is portable** - No hardcoded URLs

## Design Philosophy: Keep It Simple

The frontend tracks what's visible and polls those endpoints directly:
- ~5-6 active queries at any time (channels, messages, users, file statuses)
- Poll every 100ms when in foreground (local API, bandwidth is free)
- On mutation: immediately re-poll all visible queries
- On scroll: fetch more messages (lazy loading)

No ETags, no change tracking tables, no complex synchronization. Just make endpoints fast and idempotent.

## Typical Visible Queries

A chat app has these views that need polling:

1. **Sidebar**: `GET /networks/{id}/channels` - channel list with unread counts
2. **Main view**: `GET /networks/{id}/channels/{id}/messages?limit=50` - current messages
3. **Members panel**: `GET /networks/{id}/users` - user list
4. **File downloads**: `GET /networks/{id}/files/{id}/status` - for any active downloads
5. **Network status**: `GET /networks/{id}/sync/status` - peer connections

Frontend workflow:
```
on_foreground():
    start_polling(100ms)

on_background():
    stop_polling()

on_mutation_success():
    poll_all_visible_immediately()

on_scroll_to_top():
    fetch_older_messages(cursor=oldest_visible_id)
```

## PSK Authentication (Detailed)

The API runs locally (localhost only) for a specific peer. PSK authenticates that requests come from the legitimate frontend, not from another process.

### How It Works

1. **Startup**: Backend generates random 32-byte token, writes to `~/.quiet/{peer_id}/api_token`
2. **Frontend reads**: Desktop app reads token from same location (IPC via filesystem)
3. **Every request**: Frontend includes `Authorization: Bearer <base64_token>`
4. **Backend validates**: Rejects requests without valid token

```python
# Backend startup
token = secrets.token_bytes(32)
Path(f"~/.quiet/{peer_id}/api_token").write_bytes(token)
app.state.api_token = token

# Middleware
@app.middleware("http")
async def auth_middleware(request, call_next):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return Response(status_code=401)
    token = base64.b64decode(auth[7:])
    if not secrets.compare_digest(token, app.state.api_token):
        return Response(status_code=401)
    return await call_next(request)
```

### Why Not TLS Client Certs?

Simpler. Token file works on all platforms, no cert generation/management. Security is adequate since:
- API only binds to localhost
- Token file has restrictive permissions (0600)
- Token rotates on each startup

### Peer Context

The API server runs for ONE peer. No need to identify peer in requests - it's implicit:

```bash
# Start API for specific peer
python -m api --peer-id abc123 --port 8080

# All queries scoped to that peer automatically
GET /networks/net1/channels  # SafeDB uses peer_id=abc123
```

## Files: Progressive Download

```
GET /files/{file_id}
  - Full binary (blocks until complete)

GET /files/{file_id}/status
  - {"status": "syncing|complete|failed", "progress": 0.75, "bytes_synced": 1234, "bytes_total": 5678}

GET /files/{file_id}/data?offset=0&length=4096
  - Partial binary read (for streaming playback, previews)

POST /files/{file_id}/sync
  - {"priority": 1-10} - Request file sync with priority
  - Returns status
```

### Message Lazy Loading

```
GET /channels/{channel_id}/messages?cursor={id}&direction=older&limit=50

Response:
{
  "items": [...],
  "cursors": {
    "older": "msg_abc",  // null if at beginning
    "newer": "msg_xyz"   // null if at end
  }
}
```

Direction semantics:
- `older`: messages before cursor (scrolling up)
- `newer`: messages after cursor (catching up)
- No cursor + `older`: most recent 50

## Project Structure (Phase 1)

```
poc-6-api/
├── app/                          # React Native Web app
│   ├── src/
│   │   ├── components/           # UI components
│   │   ├── screens/              # Screen components
│   │   ├── db/
│   │   │   └── index.ts          # sql.js wrapper (read-only queries)
│   │   ├── api/
│   │   │   └── client.ts         # fetch('/api/...') wrapper
│   │   └── hooks/                # React hooks for data fetching
│   │
│   ├── cypress/
│   │   ├── component/            # Component tests
│   │   ├── e2e/                  # Full flow tests
│   │   └── support/
│   │
│   ├── vite.config.ts            # Proxy /api → Unix socket
│   └── package.json
│
├── api/                          # Python HTTP API
│   ├── __init__.py
│   ├── app.py                    # FastAPI app
│   ├── routes/                   # HTTP endpoints
│   └── api.yaml                  # OpenAPI spec
│
├── events/                       # Event modules (existing)
├── core/                         # Core infrastructure (existing)
├── cli.py                        # CLI (existing)
├── tests/                        # Python tests (existing)
│
└── docs/
    └── api-design.md             # This file
```

Note: Python backend stays at repo root (existing structure). `app/` is added for the React Native Web frontend.

## Testing Strategy (Phase 1)

| Test Type | Tool | Speed | When |
|-----------|------|-------|------|
| Component tests | Cypress + RN Web | ~1s | Every save |
| E2E (web) | Cypress + RN Web | ~5s | Every commit |
| Python unit tests | pytest | ~2s | Every commit |

**Cypress component test:**
```typescript
// cypress/component/MessageList.cy.tsx
describe('MessageList', () => {
  beforeEach(() => {
    cy.request('POST', '/api/test/seed', { fixture: 'messages' });
  });

  it('renders messages', () => {
    cy.mount(<MessageList channelId="ch1" />);
    cy.contains('Hello').should('be.visible');
  });

  it('loads more on scroll', () => {
    cy.mount(<MessageList channelId="ch1" />);
    cy.get('[data-testid="message-list"]').scrollTo('top');
    // Should trigger fetch for older messages
  });
});
```

**Cypress E2E test:**
```typescript
// cypress/e2e/send-message.cy.ts
describe('Send message flow', () => {
  beforeEach(() => {
    cy.request('POST', '/api/test/reset');
    cy.request('POST', '/api/test/seed', { fixture: 'basic-network' });
    cy.visit('/');
  });

  it('sends a message and sees it appear', () => {
    cy.get('[data-testid="channel-general"]').click();
    cy.get('[data-testid="message-input"]').type('Hello from Cypress');
    cy.get('[data-testid="send-button"]').click();
    cy.contains('Hello from Cypress').should('be.visible');
  });
});
```

**Python backend test endpoints (dev only):**
```python
@app.post("/api/test/reset")
async def reset():
    """Wipe test database"""

@app.post("/api/test/seed")
async def seed(fixture: str):
    """Load test fixture into database"""
```

## Key Implementation Details

### Authentication

PSK (pre-shared key) passed via header:
```
Authorization: Bearer <base64_psk>
```

The PSK identifies the local peer. API extracts `peer_id` from it.

### Database Access

Use the existing `SafeDB` pattern - all queries scoped by `recorded_by=peer_id`:

```python
@app.middleware("http")
async def db_context(request, call_next):
    peer_id = auth.get_peer_id(request)
    request.state.db = create_safe_db(get_db(), recorded_by=peer_id)
    return await call_next(request)
```


## Critical Files to Modify/Create

### New Files (in worktree)
- `api/app.py` - FastAPI application
- `api/routes/*.py` - Route handlers
- `api/schemas/*.py` - Pydantic models
- `api/core/*.py` - Shared infrastructure

### Existing Files (No Changes Needed)
- `core/db.py` - SafeDB pattern works as-is
- `events/*` - All event modules used read-only by API
- `core/store.py` - Event storage works as-is

### Integration Points
- Routes call existing event module functions (`message.create()`, `channel.list_channels()`, etc.)
- No direct SQL in API layer - go through event modules as CLI does
- API is a thin HTTP wrapper over the event system

## Implementation Phases

### Phase 1: Core Infrastructure
1. FastAPI app skeleton with PSK auth
2. Database middleware with SafeDB
3. Basic health/status endpoints
4. CLI entry point (`python -m api --peer-id X`)

### Phase 2: Read Endpoints (Polling Targets)
1. `GET /networks` - list networks peer belongs to
2. `GET /networks/{id}/channels` - channel list with metadata
3. `GET /networks/{id}/channels/{id}/messages` - paginated messages
4. `GET /networks/{id}/users` - user list
5. `GET /networks/{id}/files/{id}/status` - file sync status
6. `GET /networks/{id}/sync/status` - peer connections

### Phase 3: Write Endpoints
1. `POST /networks/{id}/channels/{id}/messages` - send message (multipart)
2. `POST /networks/{id}/channels` - create channel (admin)
3. `POST /networks/{id}/invites` - create invite
4. `POST /networks/join` - join via invite link
5. `DELETE /messages/{id}` - delete message
6. `POST /messages/{id}/reactions` - add reaction

### Phase 4: Files
1. `GET /files/{id}` - binary download (complete files)
2. `GET /files/{id}/data?offset=X&length=Y` - partial read
3. `POST /files/{id}/sync` - request priority sync

## Verification

1. **Unit tests**: Test each route with mocked db
2. **Integration tests**: Use existing test infrastructure (scenario tests pattern)
3. **Manual testing**:
   ```bash
   # Start server
   cd api && uvicorn app:app --port 8080

   # Test endpoints
   curl -H "Authorization: Bearer $PSK" http://localhost:8080/networks
   curl http://localhost:8080/networks/net1/channels
   curl http://localhost:8080/channels/ch1/messages?limit=10
   ```

## Design Decisions (Confirmed)

1. **Network scoping**: Keep `/networks/{id}/...` nesting - maintains spec compatibility and multi-network future

2. **Real-time**: Polling only, no SSE - simpler and more portable

3. **File upload**: Multipart form-data - standard, efficient, streaming capable

## File Upload Details

Message with attachment:
```
POST /networks/{network_id}/channels/{channel_id}/messages
Content-Type: multipart/form-data

--boundary
Content-Disposition: form-data; name="text"

Hello world
--boundary
Content-Disposition: form-data; name="attachments"; filename="photo.jpg"
Content-Type: image/jpeg

<binary data>
--boundary--
```

Response:
```json
{
  "message_id": "abc123",
  "attachments": [
    {"file_id": "file456", "filename": "photo.jpg", "status": "syncing"}
  ]
}
```
