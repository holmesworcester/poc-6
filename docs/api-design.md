# API Implementation Plan for Quiet Protocol

## Context

Building a RESTful API for mobile apps (React Native) with web-based development workflow.

**Target platforms:**
- iOS / Android via Expo (production)
- React Native Web (development + Cypress testing)

**Key requirements:**
- Polling-first (100ms intervals when foreground)
- Lazy loading (paginated messages, chunked files)
- Cypress-testable via RN Web
- Maestro for native smoke tests

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  Development (fast iteration)                               │
│                                                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐ │
│  │   Cypress   │───▶│  RN Web     │───▶│  Python Backend │ │
│  │   Tests     │    │  (browser)  │    │  (Unix socket)  │ │
│  └─────────────┘    └─────────────┘    └─────────────────┘ │
│                            │                    │           │
│                            ▼                    ▼           │
│                     ┌─────────────┐    ┌─────────────────┐ │
│                     │   sql.js    │    │     SQLite      │ │
│                     │   (WASM)    │    │     (file)      │ │
│                     └─────────────┘    └─────────────────┘ │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Production (native)                                        │
│                                                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐ │
│  │   Maestro   │───▶│  Expo App   │───▶│  Python Backend │ │
│  │   Tests     │    │  (iOS/And)  │    │  (Unix socket)  │ │
│  └─────────────┘    └─────────────┘    └─────────────────┘ │
│                            │                    │           │
│                            ▼                    ▼           │
│                     ┌─────────────┐    ┌─────────────────┐ │
│                     │  op-sqlite  │    │     SQLite      │ │
│                     │   (JSI)     │    │     (file)      │ │
│                     └─────────────┘    └─────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Local Dev: No Ports via Vite Proxy

Frontend uses relative URLs. Vite proxies to Unix socket.

```
Frontend: fetch('/api/messages')
    │
    ▼
Vite Dev Server (:5173)
    │ proxy /api/* → socket
    ▼
Python Backend (Unix socket: /tmp/quiet-dev.sock)
```

**vite.config.ts:**
```typescript
export default defineConfig({
  server: {
    proxy: {
      '/api': {
        target: { socketPath: '/tmp/quiet-dev.sock' },
      }
    }
  }
});
```

**Benefits:**
- Zero port configuration in frontend
- No CORS (same origin)
- No port conflicts
- Same code works in prod

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

## Project Structure

```
quiet/
├── app/                          # React Native app
│   ├── src/
│   │   ├── components/           # Shared UI components
│   │   ├── screens/              # Screen components
│   │   ├── db/
│   │   │   ├── index.ts          # Platform-agnostic interface
│   │   │   ├── native.ts         # op-sqlite (iOS/Android)
│   │   │   └── web.ts            # sql.js (browser)
│   │   ├── api/
│   │   │   └── client.ts         # fetch('/api/...') wrapper
│   │   └── queries/              # Shared SQL queries
│   │
│   ├── cypress/
│   │   ├── component/            # Component tests
│   │   ├── e2e/                  # Full flow tests
│   │   └── support/
│   │
│   ├── e2e/                      # Maestro tests (YAML)
│   │   └── send-message.yaml
│   │
│   ├── vite.config.ts            # Proxy config for dev
│   ├── metro.config.js           # Metro for native
│   └── package.json
│
├── backend/                       # Python backend
│   ├── api/
│   │   ├── __init__.py
│   │   ├── app.py                # FastAPI app
│   │   ├── routes/               # HTTP endpoints
│   │   └── core/                 # DB, auth
│   │
│   ├── events/                   # Event modules (existing)
│   ├── core/                     # Core infrastructure (existing)
│   └── tests/
│
└── docs/
    └── api-design.md             # This file
```

## Testing Strategy

| Test Type | Tool | Speed | When |
|-----------|------|-------|------|
| Component tests | Cypress + RN Web | ~1s | Every save |
| E2E (web) | Cypress + RN Web | ~5s | Every commit |
| Native smoke | Maestro + Expo | ~30s | CI / pre-release |

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
});
```

**Maestro native test:**
```yaml
# e2e/send-message.yaml
appId: com.quiet.app
---
- launchApp
- tapOn: "general"
- tapOn: { id: "message-input" }
- inputText: "Hello from Maestro"
- tapOn: { id: "send-button" }
- assertVisible: "Hello from Maestro"
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
