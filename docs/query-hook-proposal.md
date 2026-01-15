# Query Hook Proposal for poc-6

## Overview

This document proposes a query subscription system that balances simplicity with efficiency. It starts with a minimal viable approach, then describes graduated optimizations inspired by Rocicorp Zero's architecture.

---

## Part 1: Simple Approach (MVP)

### Core Design

1. Active queries poll every 100ms
2. Local mutations trigger immediate repoll of all active queries
3. Queries register/unregister as components mount/unmount

### Architecture

```
Local mutation (user action)  ──→  refetchAll()  ──→  instant update
Background                    ──→  poll 100ms   ──→  picks up remote events
```

## Frontend

```typescript
// ============================================
// Global query registry
// ============================================
const activeQueries = new Set<() => void>()

function refetchAll() {
  activeQueries.forEach(fn => fn())
}

// Poll all active queries every 100ms
setInterval(refetchAll, 100)

// ============================================
// API wrapper
// ============================================
export const api = {
  query: (name: string, params: object) =>
    fetch(`/query/${name}`, { method: 'POST', body: JSON.stringify(params) })
      .then(r => r.json()),

  mutate: async (name: string, params: object) => {
    await fetch(`/mutate/${name}`, { method: 'POST', body: JSON.stringify(params) })
    refetchAll()  // Immediate repoll on success
  },
}

// ============================================
// useQuery hook
// ============================================
function useQuery<T>(queryName: string, params: Record<string, any>) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)

  const refetch = useCallback(() => {
    api.query(queryName, params).then(setData)
  }, [queryName, JSON.stringify(params)])

  useEffect(() => {
    // Initial fetch
    refetch().then(() => setLoading(false))

    // Register for polling + mutation-triggered refetch
    activeQueries.add(refetch)

    return () => {
      activeQueries.delete(refetch)
    }
  }, [refetch])

  return { data, loading }
}

// ============================================
// Usage
// ============================================
function MessageList({ channelId }: { channelId: string }) {
  const { data: messages, loading } = useQuery('message.list', { channel_id: channelId })

  if (loading) return <div>Loading...</div>
  return messages.map(m => <Message key={m.id} message={m} />)
}

function MessageInput({ channelId }: { channelId: string }) {
  const [content, setContent] = useState('')

  const send = async () => {
    await api.mutate('message.create', { channel_id: channelId, content })
    // MessageList updates automatically via refetchAll()
    setContent('')
  }

  return (
    <div>
      <input value={content} onChange={e => setContent(e.target.value)} />
      <button onClick={send}>Send</button>
    </div>
  )
}
```

## Two Update Paths

- **Local mutation** — `api.mutate()` → `refetchAll()` → instant
- **Remote peer** — P2P sync delivers event → next 100ms poll picks it up

## Why This Works

- **Local actions feel instant** — mutation success triggers immediate repoll
- **Remote events appear within 100ms** — polling catches them
- **No SSE complexity** — just polling + refetch
- **Scales with UI** — only active (mounted) queries poll
- **Simple to implement** — ~30 lines of code

## Performance

With ~5 active queries polling every 100ms:
- 50 queries/sec to localhost SQLite
- ~50ms/sec SQL time (~5% CPU)
- Negligible for local-only single-user app

## Optional: Pause When Hidden

```typescript
let pollInterval: number | null = null

function startPolling() {
  if (!pollInterval) {
    pollInterval = setInterval(refetchAll, 100)
  }
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval)
    pollInterval = null
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling()
  } else {
    refetchAll()  // Immediate refresh when returning
    startPolling()
  }
})

startPolling()
```

### Summary (MVP)

| Concern | Solution |
|---------|----------|
| Remote events | Poll every 100ms |
| Local mutations | `refetchAll()` on success |
| Query lifecycle | Register on mount, unregister on unmount |
| Wiring components | None needed — global refetch handles it |
| Client state | `useState` + `useEffect` |

---

## Part 2: Table-Based Invalidation

The MVP's `refetchAll()` approach refetches every active query on any mutation. This works fine with ~5 queries, but becomes wasteful as the app grows. The first optimization: **only refetch queries that read from tables the mutation touched**.

### Core Insight

Queries and mutations can declare their table dependencies:
- Query `message.list` reads from: `messages`, `users` (for author info)
- Mutation `message.create` writes to: `messages`
- Mutation `channel.rename` writes to: `channels`

When `message.create` fires, only queries that read `messages` need refetching—not queries that only read `channels` or `settings`.

### Implementation

```typescript
// ============================================
// Table dependency registry
// ============================================
type TableName = string
type QueryKey = string

interface QueryEntry {
  refetch: () => void
  tables: Set<TableName>
}

const queryRegistry = new Map<QueryKey, QueryEntry>()

// Index: table → queries that read from it
const tableToQueries = new Map<TableName, Set<QueryKey>>()

function registerQuery(
  key: QueryKey,
  tables: TableName[],
  refetch: () => void
) {
  // Store query entry
  queryRegistry.set(key, { refetch, tables: new Set(tables) })

  // Update table index
  for (const table of tables) {
    if (!tableToQueries.has(table)) {
      tableToQueries.set(table, new Set())
    }
    tableToQueries.get(table)!.add(key)
  }
}

function unregisterQuery(key: QueryKey) {
  const entry = queryRegistry.get(key)
  if (!entry) return

  // Remove from table index
  for (const table of entry.tables) {
    tableToQueries.get(table)?.delete(key)
  }

  queryRegistry.delete(key)
}

// ============================================
// Targeted invalidation
// ============================================
function invalidateTables(tables: TableName[]) {
  const queriesToRefetch = new Set<QueryKey>()

  for (const table of tables) {
    const queries = tableToQueries.get(table)
    if (queries) {
      for (const key of queries) {
        queriesToRefetch.add(key)
      }
    }
  }

  for (const key of queriesToRefetch) {
    queryRegistry.get(key)?.refetch()
  }
}

// Fallback for unknown mutations
function refetchAll() {
  for (const entry of queryRegistry.values()) {
    entry.refetch()
  }
}

// ============================================
// API wrapper with table declarations
// ============================================
export const api = {
  query: (name: string, params: object) =>
    fetch(`/query/${name}`, { method: 'POST', body: JSON.stringify(params) })
      .then(r => r.json()),

  mutate: async (name: string, params: object, writesToTables?: TableName[]) => {
    await fetch(`/mutate/${name}`, { method: 'POST', body: JSON.stringify(params) })

    if (writesToTables) {
      invalidateTables(writesToTables)
    } else {
      refetchAll()  // Fallback if tables not specified
    }
  },
}

// ============================================
// Updated useQuery hook
// ============================================
function useQuery<T>(
  queryName: string,
  params: Record<string, any>,
  options: { tables: TableName[] }  // Declare dependencies
) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)

  // Stable key for this query instance
  const queryKey = useMemo(
    () => `${queryName}:${JSON.stringify(params)}`,
    [queryName, params]
  )

  const refetch = useCallback(() => {
    api.query(queryName, params).then(setData)
  }, [queryName, JSON.stringify(params)])

  useEffect(() => {
    refetch().then(() => setLoading(false))

    registerQuery(queryKey, options.tables, refetch)

    return () => {
      unregisterQuery(queryKey)
    }
  }, [queryKey, refetch, options.tables])

  return { data, loading, refetch }
}

// ============================================
// Usage
// ============================================
function MessageList({ channelId }: { channelId: string }) {
  const { data: messages } = useQuery(
    'message.list',
    { channel_id: channelId },
    { tables: ['messages', 'users'] }  // Reads messages + user info
  )
  // ...
}

function ChannelHeader({ channelId }: { channelId: string }) {
  const { data: channel } = useQuery(
    'channel.get',
    { channel_id: channelId },
    { tables: ['channels'] }  // Only reads channels
  )
  // ...
}

// Mutation specifies what it writes
async function sendMessage(channelId: string, content: string) {
  await api.mutate(
    'message.create',
    { channel_id: channelId, content },
    ['messages']  // Only touches messages table
  )
  // MessageList refetches (reads messages)
  // ChannelHeader does NOT refetch (only reads channels)
}
```

### Polling with Table Awareness

For remote events, we can still poll but be smarter about it. The backend can return which tables changed since last poll:

```typescript
// Backend returns: { tables_changed: ['messages'], data: [...] }
// or simpler: just poll a "changes since version X" endpoint

let lastVersion = 0

async function pollForChanges() {
  const { version, tables_changed } = await fetch(
    `/changes?since=${lastVersion}`
  ).then(r => r.json())

  if (tables_changed.length > 0) {
    invalidateTables(tables_changed)
  }

  lastVersion = version
}

setInterval(pollForChanges, 100)
```

### Cost

- ~50 additional lines of code
- O(1) lookup per table to find affected queries
- Queries must declare their table dependencies (manual, but simple)

---

## Part 3: Query Deduplication

Multiple components might render the same query (e.g., user avatar in many places). Without deduplication, each instance fires a separate request.

### Implementation

```typescript
// ============================================
// Query deduplication via reference counting
// ============================================
interface QueryEntry {
  refetch: () => void
  tables: Set<TableName>
  refCount: number  // How many subscribers
  data: any         // Shared result
  listeners: Set<(data: any) => void>
}

const queryRegistry = new Map<QueryKey, QueryEntry>()

function subscribeQuery(
  key: QueryKey,
  tables: TableName[],
  onData: (data: any) => void
): () => void {
  let entry = queryRegistry.get(key)

  if (entry) {
    // Existing query - increment ref count
    entry.refCount++
    entry.listeners.add(onData)

    // Immediately deliver cached data if available
    if (entry.data !== undefined) {
      onData(entry.data)
    }
  } else {
    // New query - create entry
    const refetch = async () => {
      const data = await api.query(key.split(':')[0], JSON.parse(key.split(':')[1]))
      entry!.data = data
      for (const listener of entry!.listeners) {
        listener(data)
      }
    }

    entry = {
      refetch,
      tables: new Set(tables),
      refCount: 1,
      data: undefined,
      listeners: new Set([onData]),
    }

    queryRegistry.set(key, entry)
    updateTableIndex(key, tables)
    refetch()  // Initial fetch
  }

  // Return unsubscribe function
  return () => {
    const e = queryRegistry.get(key)
    if (!e) return

    e.listeners.delete(onData)
    e.refCount--

    if (e.refCount === 0) {
      // Last subscriber gone - clean up
      queryRegistry.delete(key)
      removeFromTableIndex(key, e.tables)
    }
  }
}

// ============================================
// Hook using deduplication
// ============================================
function useQuery<T>(
  queryName: string,
  params: Record<string, any>,
  options: { tables: TableName[] }
) {
  const [data, setData] = useState<T | undefined>(undefined)

  const queryKey = useMemo(
    () => `${queryName}:${JSON.stringify(params)}`,
    [queryName, params]
  )

  useEffect(() => {
    return subscribeQuery(queryKey, options.tables, setData)
  }, [queryKey, options.tables])

  return { data, loading: data === undefined }
}
```

### Benefits

- 10 components showing user avatar → 1 query, not 10
- Cached data delivered immediately to new subscribers
- Memory cleaned up when last subscriber unmounts

---

## Part 4: Batching and Debouncing

Rapid mutations (e.g., typing in a search box) can trigger excessive refetches. Batching coalesces these into fewer operations.

### Implementation

```typescript
// ============================================
// Debounced invalidation
// ============================================
let pendingTables = new Set<TableName>()
let invalidationTimeout: number | null = null

function invalidateTables(tables: TableName[]) {
  for (const table of tables) {
    pendingTables.add(table)
  }

  // Debounce: wait 16ms (one frame) to collect more invalidations
  if (!invalidationTimeout) {
    invalidationTimeout = setTimeout(() => {
      const tables = Array.from(pendingTables)
      pendingTables.clear()
      invalidationTimeout = null

      doInvalidation(tables)
    }, 16)
  }
}

function doInvalidation(tables: TableName[]) {
  const queriesToRefetch = new Set<QueryKey>()

  for (const table of tables) {
    const queries = tableToQueries.get(table)
    if (queries) {
      for (const key of queries) {
        queriesToRefetch.add(key)
      }
    }
  }

  // Batch: one refetch per query, not per invalidation
  for (const key of queriesToRefetch) {
    queryRegistry.get(key)?.refetch()
  }
}
```

### Coalescing In-Flight Requests

Prevent duplicate requests for the same query:

```typescript
interface QueryEntry {
  // ... existing fields
  inflight: Promise<any> | null
}

async function refetchQuery(key: QueryKey) {
  const entry = queryRegistry.get(key)
  if (!entry) return

  // If already fetching, return existing promise
  if (entry.inflight) {
    return entry.inflight
  }

  entry.inflight = api.query(/* ... */)
    .then(data => {
      entry.data = data
      entry.inflight = null
      notifyListeners(entry)
      return data
    })
    .catch(err => {
      entry.inflight = null
      throw err
    })

  return entry.inflight
}
```

---

## Part 5: Result Stability (Shallow Comparison)

Even with deduplication, React will re-render if `data` is a new object reference. We can stabilize results to prevent unnecessary renders.

```typescript
function useStableData<T>(data: T | undefined): T | undefined {
  const ref = useRef<T | undefined>(data)

  if (data === undefined) {
    return ref.current
  }

  // Shallow compare arrays
  if (Array.isArray(data) && Array.isArray(ref.current)) {
    if (
      data.length === ref.current.length &&
      data.every((item, i) => shallowEqual(item, ref.current![i]))
    ) {
      return ref.current  // No change, return stable reference
    }
  }

  ref.current = data
  return data
}

function useQuery<T>(/* ... */) {
  const [rawData, setRawData] = useState<T | undefined>(undefined)
  const data = useStableData(rawData)
  // ...
}
```

---

## Part 6: Row-Level Invalidation (Advanced)

Table-level invalidation is coarse. If `messages` table changes but the mutation affected channel A, queries for channel B still refetch unnecessarily.

### Query Fingerprinting

Queries can declare not just tables but **filtered keys**:

```typescript
const { data } = useQuery(
  'message.list',
  { channel_id: channelId },
  {
    tables: ['messages'],
    filters: { messages: { channel_id: channelId } }  // Row-level filter
  }
)
```

### Invalidation with Row Context

```typescript
await api.mutate(
  'message.create',
  { channel_id: 'abc123', content: '...' },
  {
    tables: ['messages'],
    keys: { messages: { channel_id: 'abc123' } }  // Which rows affected
  }
)
```

### Matching Logic

```typescript
function queryMatchesMutation(
  queryFilters: Record<TableName, Record<string, any>>,
  mutationKeys: Record<TableName, Record<string, any>>
): boolean {
  for (const [table, keys] of Object.entries(mutationKeys)) {
    const queryFilter = queryFilters[table]
    if (!queryFilter) continue  // Query reads all rows from this table

    // Check if mutation keys match query filters
    for (const [key, value] of Object.entries(queryFilter)) {
      if (keys[key] !== undefined && keys[key] !== value) {
        return false  // Mutation is for different key
      }
    }
  }
  return true
}

function invalidateWithKeys(
  tables: TableName[],
  keys: Record<TableName, Record<string, any>>
) {
  for (const [queryKey, entry] of queryRegistry.entries()) {
    const matches = queryMatchesMutation(entry.filters ?? {}, keys)
    if (matches) {
      entry.refetch()
    }
  }
}
```

### Tradeoffs

- More precise invalidation (less wasted work)
- More complex API surface
- Requires mutations to know their affected keys
- Worth it when many queries on same table with different filters

---

## Part 7: Event-Based Invalidation (Backend Integration)

Instead of clients specifying dependencies, let the backend emit invalidation hints.

### Backend Emits Changes

When events are projected, emit which tables/rows changed:

```python
# In projection code
def project_message_created(event, db):
    db.execute("INSERT INTO messages ...")

    # Emit invalidation hint
    emit_change('messages', {'channel_id': event['channel_id']})
```

### Frontend Consumes via SSE/WebSocket

```typescript
const eventSource = new EventSource('/changes')

eventSource.onmessage = (event) => {
  const { table, keys } = JSON.parse(event.data)
  invalidateWithKeys([table], { [table]: keys })
}
```

### Benefits

- No manual dependency declaration needed
- Single source of truth (backend projections)
- Works for remote events too (no polling needed)

### Implementation Complexity

- Requires SSE/WebSocket infrastructure
- Backend must track and emit changes
- May not be worth it for local-first app where sync is primary path

---

## Comparison: Approaches by Complexity

| Approach | Lines of Code | Precision | Complexity |
|----------|---------------|-----------|------------|
| MVP (refetchAll) | ~30 | All queries | Trivial |
| Table-based | ~80 | Per table | Low |
| + Deduplication | ~120 | + shared cache | Low |
| + Batching | ~150 | + coalesced | Medium |
| + Row-level | ~200 | Per row key | Medium |
| + Backend events | ~250 | Automatic | High |

## Recommendation

For poc-6:

1. **Start with MVP** — get the basics working
2. **Add table-based invalidation** when you have >10 active queries or notice wasted refetches
3. **Add deduplication** when same query appears in multiple components
4. **Skip row-level** unless you have many queries on same table with different filters

The polling + local mutation immediate-refetch model works well for local-first apps. SSE-based push is more appropriate for traditional client-server apps where the server is the source of truth.

---

## Appendix: Lessons from Rocicorp Zero

Zero's architecture provides several insights relevant to poc-6:

### What Zero Does

1. **AST-based query analysis** — Queries are parsed into ASTs, normalized, and hashed for deduplication
2. **Incremental View Maintenance (IVM)** — Results computed incrementally, not re-queried
3. **Reference counting** — Queries track subscriber count, cleaned up at zero
4. **Watermark sync** — Clients track "seen up to version X" to avoid re-sending
5. **Batched patches** — Query changes sent as delta patches, throttled ~100ms

### What We Can Borrow

| Zero Pattern | poc-6 Adaptation |
|--------------|------------------|
| Query hashing | `${queryName}:${JSON.stringify(params)}` |
| Reference counting | Track subscriber count per query |
| Batching/throttling | Debounce invalidations ~16ms |
| Table dependencies | Manual declaration in useQuery |

### What We Don't Need

| Zero Feature | Why Not for poc-6 |
|--------------|-------------------|
| Full AST parsing | Overkill — our queries are simple |
| IVM operators | We re-query SQLite (fast enough) |
| Watermark sync | Sync layer handles this separately |
| CVR caching | SQLite is our cache |

### Key Insight

Zero optimizes for scale (many clients, complex queries, large datasets). poc-6 is local-first single-user. The right amount of optimization is table-level invalidation + deduplication — simple to implement, big wins, minimal complexity.
