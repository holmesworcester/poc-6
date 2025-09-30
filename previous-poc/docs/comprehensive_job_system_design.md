# Jobs and Reflectors System Design

## Core Architecture

Two similar but distinct patterns:

### Jobs (Time-triggered)
```python
def job_name(state: Dict, db: ReadOnlyConnection, time_now_ms: int) -> Tuple[bool, Dict, List[Dict]]:
    """
    Args:
        state: Persistent state from last run
        db: Read-only database connection for queries
        time_now_ms: Current timestamp in milliseconds
    Returns:
        (success, new_state, envelopes_to_emit)
    """
```

### Reflectors (Event-triggered)
```python
def reflector_name(envelope: Dict, db: ReadOnlyConnection, time_now_ms: int) -> Tuple[bool, List[Dict]]:
    """
    Args:
        envelope: The complete triggering event envelope
        db: Read-only database connection for queries
        time_now_ms: Current timestamp in milliseconds
    Returns:
        (success, envelopes_to_emit)
    """
```

Jobs are scheduled tasks that maintain state between runs. Reflectors are reactive functions that respond to events. Both are executed by `RunJobsHandler` which manages scheduling and execution.

## Complete Job Inventory

### 1. Sync Jobs

#### sync_request_job (Job)
**Frequency**: 3000ms
**State**: Per-peer sync windows, bloom filters, last sync times
```python
def sync_request_job(state: Dict, db: ReadOnlyConnection, time_now_ms: int):
    try:
        # Initialize state
        if not state:
            state = {'peer_states': {}, 'total_events': 0, 'network_id': 'test-network'}

        # Count total events for window sizing
        total_events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    window_bits = calculate_window_bits(total_events)  # 12 bits = 4096 windows initially

    # Get active peers
    peers = db.execute("""
        SELECT peer_id, ip, port FROM addresses
        WHERE network_id = ? AND is_active = 1
    """, (state['network_id'],))

    envelopes = []
    for peer in peers:
        peer_state = state['peer_states'].get(peer['peer_id'], {
            'last_window': -1,
            'windows_visited': []
        })

        # Pseudo-random walk through windows
        next_window = get_next_window_prp(
            peer_state['last_window'],
            peer_state['windows_visited'],
            window_bits
        )

        # Create bloom filter for our events in this window
        bloom = create_bloom_filter(db, next_window, window_bits)

        envelopes.append({
            'event_type': 'sync_request',
            'event_plaintext': {
                'window': next_window,
                'bloom_bits': bloom,
                'network_id': state['network_id']
            },
            'seal_to': peer['peer_id'],
            'is_outgoing': True
        })

        # Update state
        peer_state['last_window'] = next_window
        peer_state['windows_visited'].append(next_window)
        if len(peer_state['windows_visited']) >= 2**window_bits:
            peer_state['windows_visited'] = []  # Reset after full cycle

        state['peer_states'][peer['peer_id']] = peer_state

        state['total_events'] = total_events
        return True, state, envelopes  # Success
    except Exception as e:
        # Log error (in real implementation)
        print(f"Error in sync_request_job: {e}")
        return False, state, []  # Failure - preserve state, no envelopes
```

#### sync_response_reflector (Reflector)
**Trigger**: Incoming `sync_request` events
```python
def sync_response_reflector(envelope: Dict, db: ReadOnlyConnection, time_now_ms: int):
    # Don't respond to our own outgoing requests
    if envelope.get('is_outgoing'):
        return True, []

    request = envelope['event_plaintext']
    window = request['window']
    bloom = request['bloom_bits']

    # Find events in window that aren't in bloom
    events = db.execute("""
        SELECT event_id, event_blob
        FROM events
        WHERE (CAST(substr(event_id, 1, 8) AS INTEGER) >> ?) = ?
        AND NOT bloom_contains(?, event_id)
        LIMIT 100
    """, (52 - window_bits, window, bloom))

    # Generate response envelopes
    responses = []
    for event in events:
        responses.append({
            'event_id': event['event_id'],
            'event_blob': event['event_blob'],
            'seal_to': request['peer_id'],
            'is_outgoing': True,
            'in_response_to': request.get('request_id')
        })

    return True, responses  # Success, no state needed for reflectors
```

#### sync_auth_job (Scheduled)
**Trigger**: `run_job` with `job_name: sync_auth`
**Frequency**: 5000ms
**Purpose**: Prioritize syncing auth events (keys, groups, user updates)
```python
def sync_auth_job(trigger, state, db: ReadOnlyConnection):
    # Similar to sync_request but filters for auth event types
    auth_types = ['key', 'prekey', 'user', 'grant', 'remove-user', 'remove-peer']
    # Implementation similar to sync_request_job but with auth-specific queries
```

### 2. Holepunch Jobs

#### intro_generation_job (Scheduled)
**Trigger**: `run_job` with `job_name: intro_generation`
**Frequency**: 30000ms (30 seconds) or on connection failure
**Purpose**: Generate intro events to facilitate NAT holepunching between peers
```python
def intro_generation_job(trigger, state, db: ReadOnlyConnection):
    """Generate intro events for peers that need to connect."""
    # Find pairs of peers that should be connected but aren't syncing
    # This could be based on:
    # 1. Peers in same network that haven't synced recently
    # 2. Peers marked as needing introduction
    # 3. Connection failure events

    # For now, simple approach: introduce all active peers periodically
    peers = db.execute("""
        SELECT p1.peer_id as peer1_id, p1.event_id as addr1_id,
               p2.peer_id as peer2_id, p2.event_id as addr2_id
        FROM addresses p1
        JOIN addresses p2 ON p1.network_id = p2.network_id
        WHERE p1.peer_id < p2.peer_id  -- Avoid duplicates
          AND p1.is_active = 1 AND p2.is_active = 1
          AND NOT EXISTS (
              -- Haven't synced recently
              SELECT 1 FROM sync_state s
              WHERE s.peer_id = p2.peer_id
                AND s.last_sync_ms > ?
          )
        LIMIT 10  -- Don't flood with intros
    """, (current_time_ms() - 60000,))  # No sync in last minute

    intro_envelopes = []
    for pair in peers:
        intro_envelopes.append({
            'event_type': 'intro',
            'event_plaintext': {
                'address1_id': pair['addr1_id'],
                'address2_id': pair['addr2_id'],
                'nonce': generate_nonce()
            },
            'is_outgoing': True,
            # Send to both peers
            'seal_to': [pair['peer1_id'], pair['peer2_id']]
        })

    return state, intro_envelopes
```

#### holepunch_burst_job (Event-triggered)
**Trigger**: Incoming `intro` events
**Purpose**: When we receive an intro event naming two addresses (one of which should be ours), immediately send UDP bursts to the other address to punch through NAT
```python
def holepunch_burst_job(trigger, state, db: ReadOnlyConnection):
    """
    When we receive an intro, determine if we're one of the named peers
    and start sending UDP bursts to the other peer.
    """
    intro = trigger['event_plaintext']

    # Look up the two addresses named in the intro
    addr1 = db.execute("""
        SELECT peer_id, ip, port FROM addresses WHERE event_id = ?
    """, (intro['address1_id'],)).fetchone()

    addr2 = db.execute("""
        SELECT peer_id, ip, port FROM addresses WHERE event_id = ?
    """, (intro['address2_id'],)).fetchone()

    if not addr1 or not addr2:
        return state, []

    # Get our peer_id
    our_peer = db.execute("""
        SELECT peer_id FROM peers WHERE is_self = 1 LIMIT 1
    """).fetchone()

    if not our_peer:
        return state, []

    # Determine which address is ours and which is the peer's
    if our_peer['peer_id'] == addr1['peer_id']:
        peer_addr = addr2
    elif our_peer['peer_id'] == addr2['peer_id']:
        peer_addr = addr1
    else:
        # This intro isn't for us
        return state, []

    # Send UDP burst of sync events to peer
    burst_envelopes = []
    for i in range(10):  # Send 10 sync events rapidly
        burst_envelopes.append({
            'event_type': 'sync_request',
            'event_plaintext': {
                'window': 0,
                'bloom_bits': create_empty_bloom(),
                'holepunch_burst': True,
                'network_id': trigger.get('network_id')
            },
            'is_outgoing': True,
            'dest_ip': peer_addr['ip'],
            'dest_port': peer_addr['port'],
            'transport': 'udp',
            'seal_to': peer_addr['peer_id']
        })

    return state, burst_envelopes
```

### 3. Maintenance Jobs

#### prekey_replenishment_job (Scheduled)
**Trigger**: `run_job` with `job_name: prekey_replenish`
**Frequency**: 600000ms (10 minutes)
```python
def prekey_replenishment_job(trigger, state, db: ReadOnlyConnection):
    """Maintain prekey pools for each group/channel."""
    MIN_PREKEYS = 10
    BATCH_SIZE = 20

    # Check prekey counts per group
    groups = db.execute("""
        SELECT group_id, COUNT(*) as count
        FROM prekeys
        WHERE eol_ms > ?
        GROUP BY group_id
    """, (current_time_ms() + 86400000,))  # Valid for at least 1 day

    envelopes = []
    for group in groups:
        if group['count'] < MIN_PREKEYS:
            # Generate new prekeys
            for i in range(BATCH_SIZE):
                keypair = generate_keypair()
                envelopes.append({
                    'event_type': 'prekey',
                    'event_plaintext': {
                        'group_id': group['group_id'],
                        'prekey_pub': keypair['public'],
                        'eol_ms': current_time_ms() + 7 * 86400000  # 7 days
                    },
                    'is_outgoing': True
                })

                # Store private key in state (jobs can't write to DB)
                if 'prekey_secrets' not in state:
                    state['prekey_secrets'] = {}
                state['prekey_secrets'][keypair['public']] = {
                    'private': keypair['private'],
                    'eol_ms': eol_ms
                }

    return state, envelopes
```

#### ttl_cleanup_job (Scheduled)
**Trigger**: `run_job` with `job_name: ttl_cleanup`
**Frequency**: 300000ms (5 minutes)
```python
def ttl_cleanup_job(trigger, state, db: ReadOnlyConnection):
    """Delete expired events."""
    current_ms = current_time_ms()

    # Find expired events
    expired = db.execute("""
        SELECT event_id, event_type, ttl_ms
        FROM events
        WHERE created_ms + ttl_ms < ?
        LIMIT 1000
    """, (current_ms,))

    deletion_envelopes = []
    for event in expired:
        deletion_envelopes.append({
            'event_type': 'delete-message',
            'event_plaintext': {
                'message_id': event['event_id'],
                'ttl_ms': event['ttl_ms'] + 86400000  # Outlive original
            },
            'is_outgoing': False  # Local processing only
        })

    return state, deletion_envelopes
```

### 4. Forward Secrecy Jobs

#### rekey_job (Scheduled)
**Trigger**: `run_job` with `job_name: rekey`
**Frequency**: 3600000ms (1 hour)
```python
def rekey_job(trigger, state, db: ReadOnlyConnection):
    """Create rekey events for events whose keys should be purged."""
    try:
        # Find events that need rekeying (deleted or expiring soon)
        events_to_rekey = db.execute("""
            SELECT e.event_id, e.event_plaintext, e.ttl_ms, e.key_id
            FROM events e
            JOIN keys k ON e.key_id = k.key_id
            WHERE (e.deleted = 1 OR e.created_ms + e.ttl_ms < ?)
              AND NOT EXISTS (
                  SELECT 1 FROM events r
                  WHERE r.event_type = 'rekey'
                  AND r.original_event_id = e.event_id
              )
            LIMIT 100
        """, (current_time_ms() + 86400000,))  # Expiring in next day

        rekey_envelopes = []
        for event in events_to_rekey:
            # Find clean key with appropriate TTL
            new_key = db.execute("""
                SELECT key_id FROM keys
                WHERE ttl_ms >= ?
                  AND key_id != ?
                ORDER BY ttl_ms ASC
                LIMIT 1
            """, (event['ttl_ms'], event['key_id'])).fetchone()

            if new_key:
                # Deterministic nonce for idempotency
                nonce = hash(event['event_id'] + new_key['key_id'])

                rekey_envelopes.append({
                    'event_type': 'rekey',
                    'event_plaintext': {
                        'original_event_id': event['event_id'],
                        'new_key_id': new_key['key_id'],
                        'new_ciphertext': seal_with_key(new_key, nonce, event['event_plaintext'])
                    },
                    'is_outgoing': False  # Local processing
                })

        return True, state, rekey_envelopes
    except Exception as e:
        print(f"Error in rekey_job: {e}")
        return False, state, []  # Retry sooner on failure
```

#### key_purge_job (Scheduled)
**Trigger**: `run_job` with `job_name: key_purge`
**Frequency**: 3720000ms (1 hour + 20 minutes, after rekeying)
```python
def key_purge_job(trigger, state, db: ReadOnlyConnection):
    """Generate purge events for keys that have no events using them."""
    try:
        # Find keys that can be safely purged
        purgeable_keys = db.execute("""
            SELECT DISTINCT k.key_id
            FROM keys k
            WHERE NOT EXISTS (
                -- No non-rekeyed events use this key
                SELECT 1 FROM events e
                WHERE e.key_id = k.key_id
                  AND e.deleted = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM events r
                      WHERE r.event_type = 'rekey'
                      AND r.original_event_id = e.event_id
                  )
            )
            LIMIT 100
        """).fetchall()

        # Generate purge events (actual deletion handled by a handler)
        purge_envelopes = []
        for row in purgeable_keys:
            purge_envelopes.append({
                'event_type': 'purge_key',
                'event_plaintext': {
                    'key_id': row['key_id'],
                    'secure_delete': True
                },
                'is_outgoing': False  # Local only
            })

        return True, state, purge_envelopes
    except Exception as e:
        print(f"Error in key_purge_job: {e}")
        return False, state, []
```

### 5. Blob Management Jobs

#### blob_fetch_job (Event-triggered)
**Trigger**: User requests a blob (e.g., clicking on an attachment)
```python
def blob_fetch_job(trigger, state, db: ReadOnlyConnection):
    """Prioritize fetching slices for a wanted blob."""
    blob_id = trigger['blob_id']

    # Get blob metadata
    blob = db.execute("""
        SELECT blob_bytes, total_slices FROM blobs WHERE blob_id = ?
    """, (blob_id,)).fetchone()

    if not blob:
        return state, []

    # Calculate windows for this blob
    window_count = min(4096, max(1, ceil(blob['total_slices'] / 100)))

    # Track which windows we've requested
    if 'blob_windows' not in state:
        state['blob_windows'] = {}

    blob_state = state['blob_windows'].get(blob_id, {
        'last_window': -1,
        'windows_requested': []
    })

    # Get next window
    next_window = (blob_state['last_window'] + 1) % window_count

    # Create bloom of slices we already have
    existing_slices = db.execute("""
        SELECT slice_number FROM blob_slices
        WHERE blob_id = ?
    """, (blob_id,))

    bloom = create_bloom_for_slices(existing_slices)

    # Request missing slices
    envelope = {
        'event_type': 'sync-blob',
        'event_plaintext': {
            'blob_id': blob_id,
            'window': next_window,
            'bloom_bits': bloom,
            'limit': 100
        },
        'is_outgoing': True
    }

    blob_state['last_window'] = next_window
    blob_state['windows_requested'].append(next_window)
    state['blob_windows'][blob_id] = blob_state

    return state, [envelope]
```

#### blob_cleanup_job (Scheduled)
**Trigger**: `run_job` with `job_name: blob_cleanup`
**Frequency**: 1800000ms (30 minutes)
```python
def blob_cleanup_job(trigger, state, db: ReadOnlyConnection):
    """Generate deletion events for orphaned blob slices."""
    # Find slices whose parent blob event is deleted
    orphaned_slices = db.execute("""
        SELECT s.slice_id, s.blob_id
        FROM blob_slices s
        LEFT JOIN events e ON e.event_id = s.blob_id
        WHERE e.event_id IS NULL OR e.deleted = 1
    """)

    # Generate deletion events (actual deletion handled by a handler)
    deletion_envelopes = []
    for slice in orphaned_slices:
        deletion_envelopes.append({
            'event_type': 'delete_blob_slice',
            'event_plaintext': {
                'slice_id': slice['slice_id'],
                'blob_id': slice['blob_id']
            },
            'is_outgoing': False  # Local only
        })

    return state, deletion_envelopes
```

### 6. Address Broadcasting Job

#### address_broadcast_job (Scheduled)
**Trigger**: `run_job` with `job_name: address_broadcast`
**Frequency**: 30000ms (30 seconds)
```python
def address_broadcast_job(trigger, state, db: ReadOnlyConnection):
    """Broadcast our current address to peers."""
    # Get our current network info
    our_ip = trigger['params'].get('external_ip')  # From STUN or config
    our_port = trigger['params'].get('port', 8080)

    # Check if address changed
    last_broadcast = state.get('last_address', {})
    if last_broadcast.get('ip') == our_ip and last_broadcast.get('port') == our_port:
        return state, []  # No change

    # Create address event
    envelope = {
        'event_type': 'address',
        'event_plaintext': {
            'transport': 1,  # UDP
            'addr': our_ip,
            'port': our_port
        },
        'is_outgoing': True
    }

    state['last_address'] = {'ip': our_ip, 'port': our_port}
    return state, [envelope]
```

## RunJobsHandler Implementation

```python
class RunJobsHandler(Handler):
    """Executes jobs and reflectors."""

    def __init__(self):
        # Load job and reflector functions (auto-discovery like validators/projectors)
        self.jobs = self._load_jobs()
        self.reflectors = self._load_reflectors()

    def _load_jobs(self):
        """Load job functions from protocols/quiet/jobs/"""
        jobs = {}
        # Would auto-discover from files, but for now:
        jobs['sync_request'] = sync_request_job
        jobs['sync_auth'] = sync_auth_job
        jobs['intro_generation'] = intro_generation_job
        jobs['prekey_replenish'] = prekey_replenishment_job
        jobs['ttl_cleanup'] = ttl_cleanup_job
        jobs['rekey'] = rekey_job
        jobs['key_purge'] = key_purge_job
        jobs['blob_cleanup'] = blob_cleanup_job
        jobs['address_broadcast'] = address_broadcast_job
        return jobs

    def _load_reflectors(self):
        """Load reflector functions from protocols/quiet/reflectors/"""
        reflectors = {}
        reflectors['sync_request'] = sync_response_reflector
        reflectors['intro'] = holepunch_burst_reflector
        reflectors['blob_wanted'] = blob_fetch_reflector
        return reflectors

    def filter(self, envelope: Dict) -> bool:
        # Handle scheduled jobs
        if envelope.get('event_type') == 'run_job':
            return envelope.get('job_name') in self.jobs

        # Handle reflectors
        return envelope.get('event_type') in self.reflectors

    def process(self, envelope: Dict, db: Connection) -> List[Dict]:
        time_now_ms = int(time.time() * 1000)

        # Create read-only connection
        from core.db import get_readonly_connection
        readonly_db = get_readonly_connection(db)

        if envelope.get('event_type') == 'run_job':
            # Run a job
            job_name = envelope['job_name']
            job_fn = self.jobs[job_name]

            # Load state for this job
            cursor = db.execute("""
                SELECT state_json FROM job_states WHERE job_name = ?
            """, (job_name,))
            row = cursor.fetchone()
            state = json.loads(row['state_json']) if row else {}

            # Run job with state
            success, new_state, envelopes = job_fn(state, readonly_db, time_now_ms)

            if success:
                # Save state
                db.execute("""
                    INSERT OR REPLACE INTO job_states (job_name, state_json, updated_ms)
                    VALUES (?, ?, ?)
                """, (job_name, json.dumps(new_state), time_now_ms))

                # Track success
                db.execute("""
                    UPDATE job_runs
                    SET last_success_ms = ?, success_count = success_count + 1
                    WHERE job_name = ?
                """, (time_now_ms, job_name))

                return envelopes
            else:
                # Job failed - track failure
                db.execute("""
                    UPDATE job_runs
                    SET last_failure_ms = ?, failure_count = failure_count + 1
                    WHERE job_name = ?
                """, (time_now_ms, job_name))

                return []  # Don't emit on failure

        else:
            # Run a reflector
            reflector_fn = self.reflectors[envelope['event_type']]

            # Reflectors don't have state, just process the envelope
            success, envelopes = reflector_fn(envelope, readonly_db, time_now_ms)

            if success:
                return envelopes
            else:
                # Log failure (reflectors don't track state)
                print(f"Reflector {envelope['event_type']} failed")
                return []
```

## Job Configuration

```yaml
# protocols/quiet/jobs.yaml
scheduled_jobs:
  - name: sync_request
    frequency_ms: 3000
    params:
      network_id: "${NETWORK_ID}"

  - name: sync_auth
    frequency_ms: 5000
    params:
      network_id: "${NETWORK_ID}"

  - name: intro_generation
    frequency_ms: 30000
    params:
      network_id: "${NETWORK_ID}"

  - name: prekey_replenish
    frequency_ms: 600000

  - name: ttl_cleanup
    frequency_ms: 300000

  - name: rekey
    frequency_ms: 3600000  # Every hour

  - name: key_purge
    frequency_ms: 4800000  # 1 hour 20 minutes (20 min after rekey)

  - name: blob_cleanup
    frequency_ms: 1800000

  - name: address_broadcast
    frequency_ms: 30000
    params:
      external_ip: "${EXTERNAL_IP}"
      port: "${PORT}"

event_triggered_jobs:
  - trigger: sync_request
    handler: sync_response

  - trigger: intro
    handler: holepunch_burst

  - trigger: blob_wanted
    handler: blob_fetch
```

## Key Design Points

1. **Two Distinct Patterns**:
   - **Jobs**: `(state, db, time_now) -> (success, new_state, envelopes)` - Scheduled, stateful
   - **Reflectors**: `(envelope, db, time_now) -> (success, envelopes)` - Event-triggered, stateless

2. **State Management**: Jobs maintain state between runs, reflectors are pure functions

3. **Read-Only DB Access**: Both get read-only database access, all mutations via envelopes

4. **Trigger Types**:
   - Jobs: Triggered by `run_job` envelopes from scheduler
   - Reflectors: Triggered by specific event types they subscribe to

5. **Success/Failure Tracking**: Both return success for retry/monitoring

6. **Clean Separation**: Jobs don't receive envelopes, reflectors don't maintain state

7. **Examples**:
   - Jobs: sync_request (periodic), rekey (periodic), ttl_cleanup (periodic)
   - Reflectors: sync_response (responds to sync_request), holepunch_burst (responds to intro)

8. **Local vs Network**: Some jobs emit local-only envelopes (ttl_cleanup), others go to network

## Database Schema

```sql
-- Job state storage
CREATE TABLE job_states (
    job_name TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);

-- Track scheduled job runs
CREATE TABLE job_runs (
    job_name TEXT PRIMARY KEY,
    last_run_ms INTEGER NOT NULL,
    last_success_ms INTEGER,
    last_failure_ms INTEGER,
    run_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0
);

-- Local state for sync
CREATE TABLE sync_state (
    peer_id TEXT PRIMARY KEY,
    last_window INTEGER,
    windows_visited TEXT,  -- JSON array
    last_sync_ms INTEGER
);

-- Local prekey secrets
CREATE TABLE local_prekey_secrets (
    prekey_pub BLOB PRIMARY KEY,
    prekey_priv BLOB NOT NULL,
    eol_ms INTEGER NOT NULL
);
```

## Summary

This unified job system handles:
- **Periodic sync** with stateful window management
- **NAT traversal** via intro/holepunch events
- **Maintenance** (TTL cleanup, blob cleanup)
- **Security** (prekey replenishment, forward secrecy via rekeying)
- **Network awareness** (address broadcasting)

All through a consistent pattern where jobs are functions that receive triggers, maintain state, and emit envelopes.
