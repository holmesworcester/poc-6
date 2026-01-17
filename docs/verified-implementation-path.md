# Verified Implementation Path: Rust + Verus + nom

This document outlines an approach for creating a formally verified implementation of the Quiet protocol, targeting iOS, Android, and desktop platforms.

## Overview

The goal is to move from the current Python prototype + TLA+ specification to a verified Rust implementation that provides strong security guarantees while remaining practical to build and maintain.

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Native UI Layer                          │
├─────────────┬─────────────────────────────────┬─────────────────┤
│   SwiftUI   │        Jetpack Compose          │   Tauri / Qt    │
│    (iOS)    │          (Android)              │    (Desktop)    │
└──────┬──────┴───────────────┬─────────────────┴────────┬────────┘
       │                      │                          │
       └──────────────────────┼──────────────────────────┘
                              │ UniFFI bindings
                      ┌───────▼───────┐
                      │  Rust Core    │
                      │               │
                      │ ┌───────────┐ │
                      │ │  Verus    │ │  ← Verified state machine
                      │ │  Proofs   │ │
                      │ └───────────┘ │
                      │               │
                      │ ┌───────────┐ │
                      │ │   nom     │ │  ← Verified parsers
                      │ │  Parsers  │ │
                      │ └───────────┘ │
                      │               │
                      │ ┌───────────┐ │
                      │ │   ring    │ │  ← Audited crypto
                      │ └───────────┘ │
                      └───────────────┘
```

### Toolchain

| Component | Tool | Purpose |
|-----------|------|---------|
| Language | Rust | Memory safety, cross-platform, zero-cost abstractions |
| Verification | Verus | Prove functional correctness |
| Parsing | nom | Parser combinators, langsec approach |
| Crypto | ring / HACL* | Audited/verified cryptographic primitives |
| Bindings | UniFFI | Generate Swift/Kotlin bindings |
| Desktop | Tauri | Native desktop apps |

## What Verus Provides

Verus is a verification tool for Rust that uses an SMT solver (Z3) to prove properties about code.

### Guarantees

```rust
verus! {
    fn project_message(ctx: &Context, event: &MessageEvent) -> (result: Option<WriteOp>)
        requires
            ctx.channels.contains_key(&event.channel_id),
            ctx.valid_signer(&event.signed_by),
        ensures
            result.is_some() ==> {
                &&& result.unwrap().table == "messages"
                &&& result.unwrap().key == event.event_id
            },
    {
        // Implementation here
        // Z3 proves postconditions hold
    }
}
```

**What Verus proves:**
- If preconditions hold, postconditions will hold
- No panics or undefined behavior
- Memory safety (inherited from Rust)
- Integer overflow protection
- Array bounds checking
- Functional correctness (code does what spec says)

**What Verus does NOT prove:**
- That your specification is correct (you might specify the wrong thing)
- Cryptographic security (assumes primitives are sound)
- Side-channel resistance
- Concurrency correctness (limited support)
- Compiler correctness (trusts rustc)

### Verus Strengths

- **Automatic proof discharge:** Z3 finds proofs automatically for many properties
- **Fast feedback:** Proofs check in seconds for most functions
- **Zero runtime cost:** Specifications are erased at compile time
- **Full Rust ecosystem:** Use any Rust library alongside verified code

### Verus Limitations

- **SMT timeouts:** Complex properties can cause Z3 to hang
- **Quantifier sensitivity:** `forall` and `exists` can be tricky
- **Inductive proofs:** Need manual lemmas for recursive structures
- **Limited higher-order reasoning:** Can't easily express properties about functions

### Concurrency: A Non-Issue for This Protocol

Verus has limited support for concurrent code, but this doesn't matter for the Quiet protocol because **we don't need concurrency in the core logic**.

**Why single-threaded is correct:**

1. **Event sourcing requires atomicity:** Each event must be processed atomically (check deps → verify → project). Concurrent event processing would require complex locking and could violate invariants.

2. **Deterministic replay:** The event log must produce the same state when replayed. Concurrent processing introduces non-determinism.

3. **Sync protocol is sequential:** Events arrive, get validated, get projected. This is naturally a single-threaded event loop.

**Architecture:**

```
┌─────────────────────────────────────────────────────────────────┐
│                        Single Thread                             │
│                                                                  │
│   Network I/O ──→ Event Queue ──→ Process Event ──→ State       │
│       ↑              (FIFO)         (atomic)         (SQLite)   │
│       │                                                          │
│       └──────────────── Send Events ─────────────────────────────│
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**What runs concurrently (outside verified core):**
- Network I/O (async, but events are queued)
- UI updates (separate thread, reads state via snapshots)
- File transfers (background, separate from event processing)

**What runs single-threaded (verified core):**
- Event verification
- Dependency checking
- Projection to database
- State queries

This means Verus can verify the entire protocol core without reasoning about concurrency. The concurrent parts (I/O, UI) are outside the verified boundary and interact only through well-defined interfaces (event queue in, state snapshots out).

```rust
// The verified core is pure and single-threaded
verus! {
    fn process_event(state: &mut State, event: &Event) -> Result<(), ProtocolError>
        requires
            state.invariants_hold(),
        ensures
            result.is_ok() ==> state.invariants_hold(),
    {
        // All verification happens here, no concurrency
        event.check_deps(state)?;
        event.verify(state)?;
        state.apply(event.project(state));
        Ok(())
    }
}

// Outside verified boundary: async I/O queues events
async fn network_loop(event_queue: Sender<Event>) {
    loop {
        let bytes = socket.recv().await;
        let event = parse_event(&bytes)?;
        event_queue.send(event).await;  // Hand off to single-threaded core
    }
}
```

**Bottom line:** Verus's concurrency limitation is irrelevant because the correct architecture for an event-sourced protocol is single-threaded event processing anyway. This is a case where the verification tool's constraints align with good system design.

### Comparison with Lean 4

| Aspect | Verus | Lean 4 |
|--------|-------|--------|
| Automation | High (SMT solver) | Lower (manual tactics) |
| Expressiveness | Refinement types | Full dependent types |
| Can prove | Most protocol properties | Anything mathematical |
| Struggles with | Nested quantifiers, induction | Nothing (but more manual work) |
| Runtime | Native Rust (zero overhead) | Lean runtime (RC overhead) |
| Ecosystem | Full Rust ecosystem | Smaller, growing |
| Learning curve | Moderate | Steep |

**Recommendation:** Start with Verus. It handles 80% of needed properties with 30% of the effort. If you hit properties Verus can't handle (complex induction, cross-peer consistency), consider a Lean model that the Rust implementation refines.

## The Bootstrap Problem

### The Challenge

Establishing trust requires identity, but identity requires trusted operations to establish.

### Current Design (Polymorphic Signers)

```python
invite:
    signed_by: network_id | peer_shared_id
    signer_type: 'network' | 'peer_shared'
```

One event type with multiple valid signer types. Verification requires runtime dispatch:

```python
def verify_invite(event):
    if event.signer_type == 'network':
        return verify_network_sig(event)
    elif event.signer_type == 'peer_shared':
        return verify_peer_shared_sig(event) and is_admin(event.signed_by)
```

**Problems for verification:**
- Case analysis over (event types × signer types)
- Runtime dispatch obscures verification logic
- Easy to miss cases

### Recommended Design (Split Event Types)

Separate bootstrap events from ongoing events:

```rust
// Genesis events (signed by network only)
InviteGenesis { signed_by: NetworkId, ... }
AdminGenesis { signed_by: NetworkId, ... }
GroupGenesis { signed_by: NetworkId, ... }

// Ongoing events (signed by peer_shared only)
Invite { signed_by: PeerSharedId, ... }  // requires admin
Admin { signed_by: PeerSharedId, ... }   // requires admin
Group { signed_by: PeerSharedId, ... }
```

**Verification becomes type-directed:**

```rust
verus! {
    fn verify_invite_genesis(event: &InviteGenesis, ctx: &Context) -> bool {
        verify_network_sig(event, ctx.network_pubkey)
        // Always this one rule
    }

    fn verify_invite(event: &Invite, ctx: &Context) -> bool {
        let signer = ctx.peer_shared.get(&event.signed_by)?;
        verify_peer_shared_sig(event, signer) && is_admin(signer.user_id, ctx)
        // Always this one rule
    }
}
```

### Unified Projection Table

Both event types project to the same table:

```
InviteGenesis event ──→ invites table
Invite event ────────→ invites table

User event ──→ depends on ──→ invites table (uniform lookup)
```

Dependents don't need to know which type created the invite:

```rust
verus! {
    fn verify_user(event: &UserEvent, ctx: &Context) -> bool
        requires
            ctx.invites.contains_key(&event.invite_id),
    {
        let invite = ctx.invites.get(&event.invite_id);
        verify_sig(event, invite.pubkey)
        // No case split on invite type!
    }
}
```

### Trust Chain

The trust chain is established by event verification:

```
InviteGenesis ──[signed by network]──→ valid
     │
     └──→ projects to invites table

User ──[signed by invite]──→ valid (invite already validated)
     │
     └──→ projects to users table

PeerShared ──[references user]──→ valid (user already validated)
     │
     └──→ projects to peers_shared table

Invite ──[signed by peer_shared, requires admin]──→ valid
     │
     └──→ projects to invites table (same table as genesis!)
```

Each event type has one verification rule. The inductive trust chain emerges from the dependency DAG.

### Schema Change Summary

```
Before: 38 events, some with signer_type polymorphism
After:  41 events (add 3 genesis variants), no polymorphism

New event types:
- invite_genesis (network-signed)
- admin_genesis (network-signed)
- group_genesis (network-signed)

Removed field:
- signer_type (no longer needed)
```

### Key Insight: Signer Polymorphism vs Dependency Polymorphism

This is the critical distinction that makes splitting event types effective.

#### Signer Polymorphism (Bad for Verification)

When the **same event type** can be signed by **different signer types**, verification requires runtime dispatch:

```rust
// BAD: Polymorphic signer within one type
fn verify_invite(event: &InviteEvent) -> bool {
    match event.signer_type {
        SignerType::Network => {
            // Path A: verify against network pubkey
            verify_network_sig(event)
        }
        SignerType::PeerShared => {
            // Path B: verify against peer_shared, check admin
            let signer = lookup_peer_shared(event.signed_by)?;
            verify_peer_shared_sig(event, signer) && is_admin(signer.user_id)
        }
    }
}
```

**Problems:**
- Two verification paths in one function
- Proof must cover both cases
- Easy to forget a case or get logic wrong
- Complexity: O(event_types × signer_types)

#### Dependency Polymorphism (Fine for Verification)

When an event **depends on** something that could have been created by **different event types**, but the dependency is resolved via a **unified table**:

```rust
// GOOD: User depends on "an invite" (either type)
fn verify_user(event: &UserEvent, ctx: &Context) -> bool {
    // Single lookup in unified invites table
    let invite = ctx.invites.get(&event.invite_id)?;

    // Single verification path
    verify_sig(event, invite.pubkey)
}
```

**Why this works:**
- Both `InviteGenesis` and `Invite` project to the **same `invites` table**
- The `User` event doesn't know or care which type created the invite
- Verification is uniform: look up by ID, get pubkey, verify signature
- The trust chain is already established when the invite was verified

#### The Pattern

```
┌─────────────────────────────────────────────────────────────────┐
│                     EVENT LAYER (Split)                          │
│                                                                  │
│   InviteGenesis                         Invite                   │
│   ─────────────                         ──────                   │
│   verify: check network sig             verify: check peer_shared│
│                                                  + admin check   │
│           │                                   │                  │
│           │ project                           │ project          │
│           ▼                                   ▼                  │
├─────────────────────────────────────────────────────────────────┤
│                   PROJECTION LAYER (Unified)                     │
│                                                                  │
│                        invites table                             │
│                   ┌──────────────────┐                          │
│                   │ id | pubkey | .. │                          │
│                   └──────────────────┘                          │
│                            ▲                                     │
│                            │ lookup by id                        │
├─────────────────────────────────────────────────────────────────┤
│                   DEPENDENT LAYER (Uniform)                      │
│                                                                  │
│   User                                                           │
│   ────                                                           │
│   verify: lookup invite by id, check sig against invite.pubkey   │
│           (no case split needed!)                                │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

#### Formal Statement

```
Signer polymorphism:
  ∃ event type T, signer types S₁ ≠ S₂:
    T can be signed by S₁ OR S₂
  → verify_T requires case split on signer type
  → BAD

Dependency polymorphism:
  ∃ event type T, dependency types D₁ ≠ D₂:
    T depends on table X
    D₁ projects to X
    D₂ projects to X
  → verify_T does uniform lookup in X
  → GOOD (no case split in T's verifier)
```

#### Why This Isn't Just Moving the Problem

You might think: "We still have two ways to create an invite, so haven't we just moved the complexity?"

**No, because:**

1. **Verification is per-event-type:** Each event type (InviteGenesis, Invite) has exactly one verification rule. The rules are proven separately.

2. **Trust is transitive via tables:** When `InviteGenesis` projects to `invites`, we've proven it's valid. When `Invite` projects to `invites`, we've proven it's valid. Anything in `invites` is therefore valid.

3. **Dependents inherit trust:** `User` doesn't re-verify the invite's authorization. It just checks the signature against the already-trusted pubkey.

4. **Complexity is additive, not multiplicative:**
   - Before: O(event_types × signer_types) verification paths
   - After: O(event_types) verification paths, each with one rule

#### Code Comparison

**Before (Polymorphic Signer):**
```rust
// Must prove: both branches establish trust chain
fn verify_invite(e: &InviteEvent, ctx: &Context) -> bool {
    match e.signer_type {
        Network => /* prove: network sig valid */,
        PeerShared => /* prove: peer_shared sig valid AND admin check */,
    }
}

// Must prove: works regardless of which signer type created the invite
fn verify_user(e: &UserEvent, ctx: &Context) -> bool {
    let invite = ctx.invites.get(&e.invite_id)?;
    match invite.signer_type {  // CASE SPLIT LEAKS TO DEPENDENT
        Network => /* ... */,
        PeerShared => /* ... */,
    }
}
```

**After (Split Types, Unified Tables):**
```rust
// Prove once: network sig valid
fn verify_invite_genesis(e: &InviteGenesis, ctx: &Context) -> bool {
    verify_network_sig(e, ctx.network_pubkey)
}

// Prove once: peer_shared sig valid AND admin check
fn verify_invite(e: &Invite, ctx: &Context) -> bool {
    let signer = ctx.peers_shared.get(&e.signed_by)?;
    verify_peer_shared_sig(e, signer) && is_admin(signer.user_id, ctx)
}

// NO CASE SPLIT - uniform lookup
fn verify_user(e: &UserEvent, ctx: &Context) -> bool {
    let invite = ctx.invites.get(&e.invite_id)?;  // Don't care which type
    verify_sig(e, invite.pubkey)                   // Same verification always
}
```

The dependent (`User`) is completely isolated from the signer polymorphism. This is the key win.

## Langsec Parsing with nom

### Philosophy

"The input language defines the attack surface."

If your parser accepts exactly the grammar (no more, no less), entire vulnerability classes vanish:
- Buffer overflows
- Injection attacks
- Malformed input bugs
- Parser differential attacks

### nom Parser Combinators

```rust
use nom::{
    bytes::complete::{tag, take},
    number::complete::{be_u32, be_u64},
    sequence::tuple,
    IResult,
};

// Parser for MessageEvent wire format
fn parse_message_event(input: &[u8]) -> IResult<&[u8], MessageEvent> {
    let (input, _) = tag(b"MSG")(input)?;           // Magic bytes
    let (input, version) = be_u32(input)?;          // Version
    let (input, channel_id) = parse_event_id(input)?;
    let (input, author_id) = parse_event_id(input)?;
    let (input, content_len) = be_u32(input)?;

    // Enforce maximum content length
    if content_len > MAX_CONTENT_LEN {
        return Err(nom::Err::Failure(Error::ContentTooLarge));
    }

    let (input, content) = take(content_len)(input)?;
    let (input, created_at) = be_u64(input)?;
    let (input, signature) = parse_signature(input)?;

    Ok((input, MessageEvent {
        channel_id,
        author_id,
        content: content.to_vec(),
        created_at,
        signature,
    }))
}

// Event ID is fixed 32 bytes
fn parse_event_id(input: &[u8]) -> IResult<&[u8], EventId> {
    let (input, bytes) = take(32usize)(input)?;
    Ok((input, EventId(bytes.try_into().unwrap())))
}
```

### Benefits

1. **Declarative:** Parser structure mirrors grammar
2. **Composable:** Build complex parsers from simple ones
3. **Bounded:** Explicit length checks prevent overflows
4. **Invertible:** Can generate serializers from same grammar
5. **Testable:** Property-based testing with arbitrary inputs

### Integration with Verus

```rust
verus! {
    // Prove parser only accepts valid lengths
    fn parse_content(input: &[u8]) -> (result: Option<(Vec<u8>, &[u8])>)
        ensures
            result.is_some() ==> result.unwrap().0.len() <= MAX_CONTENT_LEN,
    {
        // nom parsing with length check
    }
}
```

## Verified Crypto

### Option A: ring (Recommended for Practicality)

```rust
use ring::{
    signature::{Ed25519KeyPair, UnparsedPublicKey, ED25519},
    aead::{Aead, LessSafeKey, Nonce, AES_256_GCM},
    agreement::{X25519, EphemeralPrivateKey, PublicKey},
};
```

**Status:** Heavily audited, used in production (Cloudflare, Firefox), not formally verified.

### Option B: HACL* via FFI (Maximum Assurance)

```rust
// Link to HACL* C library
#[link(name = "hacl")]
extern "C" {
    fn Hacl_Ed25519_sign(
        signature: *mut u8,
        private_key: *const u8,
        msg_len: u32,
        msg: *const u8,
    );

    fn Hacl_Ed25519_verify(
        public_key: *const u8,
        msg_len: u32,
        msg: *const u8,
        signature: *const u8,
    ) -> bool;
}

// Safe Rust wrapper
pub fn ed25519_verify(public_key: &[u8; 32], msg: &[u8], sig: &[u8; 64]) -> bool {
    unsafe {
        Hacl_Ed25519_verify(
            public_key.as_ptr(),
            msg.len() as u32,
            msg.as_ptr(),
            sig.as_ptr(),
        )
    }
}
```

**Status:** Formally verified in F*, extracts to C, used in Firefox/Linux/WireGuard.

### HACL* Coverage

| Primitive | HACL* | ring |
|-----------|-------|------|
| Ed25519 | ✓ Verified | ✓ Audited |
| X25519 | ✓ Verified | ✓ Audited |
| AES-GCM | ✓ Verified | ✓ Audited |
| ChaCha20-Poly1305 | ✓ Verified | ✓ Audited |
| SHA-256/512 | ✓ Verified | ✓ Audited |
| BLAKE2 | ✓ Verified | ✓ Audited |
| HKDF | ✓ Verified | ✓ Audited |

**Recommendation:** Start with ring for faster development. HACL* can be swapped in later for higher assurance without API changes.

## Event System Design

### Event Trait

```rust
verus! {
    pub trait Event: Sized {
        type SignerType;

        fn event_type(&self) -> &'static str;
        fn event_id(&self) -> EventId;
        fn signed_by(&self) -> &Self::SignerType;
        fn created_at(&self) -> u64;

        // Parse from wire format
        fn parse(input: &[u8]) -> Option<Self>;

        // Serialize to wire format
        fn serialize(&self) -> Vec<u8>;

        // Check if dependencies are satisfied
        fn check_deps(&self, ctx: &Context) -> bool;

        // Project to database writes
        fn project(&self, ctx: &Context) -> Vec<WriteOp>
            requires
                self.check_deps(ctx),
            ensures
                // Projection produces valid writes
                forall |w: WriteOp| result.contains(&w) ==> w.is_valid();
    }
}
```

### Concrete Event Types

```rust
verus! {
    // Genesis event: signed by network
    pub struct InviteGenesis {
        pub event_id: EventId,
        pub signed_by: NetworkId,
        pub invite_pubkey: PublicKey,
        pub created_at: u64,
        pub signature: Signature,
    }

    impl Event for InviteGenesis {
        type SignerType = NetworkId;

        fn check_deps(&self, ctx: &Context) -> bool {
            // No dependencies for genesis
            true
        }

        fn project(&self, ctx: &Context) -> Vec<WriteOp>
            requires self.check_deps(ctx),
        {
            vec![WriteOp::Insert {
                table: "invites",
                key: self.event_id,
                values: invite_row(self),
            }]
        }
    }

    // Ongoing event: signed by peer_shared
    pub struct Invite {
        pub event_id: EventId,
        pub signed_by: PeerSharedId,
        pub invite_pubkey: PublicKey,
        pub created_at: u64,
        pub signature: Signature,
    }

    impl Event for Invite {
        type SignerType = PeerSharedId;

        fn check_deps(&self, ctx: &Context) -> bool {
            // Must be signed by an admin
            ctx.peers_shared.contains_key(&self.signed_by)
                && ctx.is_admin(ctx.peers_shared[&self.signed_by].user_id)
        }

        fn project(&self, ctx: &Context) -> Vec<WriteOp>
            requires self.check_deps(ctx),
        {
            vec![WriteOp::Insert {
                table: "invites",  // Same table as genesis!
                key: self.event_id,
                values: invite_row(self),
            }]
        }
    }
}
```

### Dependency Resolution

```rust
verus! {
    pub struct Context {
        pub network_id: NetworkId,
        pub network_pubkey: PublicKey,
        pub valid_events: HashSet<EventId>,
        pub invites: HashMap<EventId, InviteRow>,
        pub users: HashMap<EventId, UserRow>,
        pub peers_shared: HashMap<PeerSharedId, PeerSharedRow>,
        pub admins: HashSet<UserId>,
        pub channels: HashMap<EventId, ChannelRow>,
        pub groups: HashMap<EventId, GroupRow>,
    }

    impl Context {
        pub fn is_admin(&self, user_id: UserId) -> bool {
            self.admins.contains(&user_id)
        }

        pub fn valid_signer(&self, peer_shared_id: &PeerSharedId) -> bool {
            self.peers_shared.contains_key(peer_shared_id)
        }
    }
}
```

## Verification Strategy

### Phase 1: Parser Verification (Weeks)

Prove parsers accept exactly valid input:

```rust
verus! {
    fn parse_event_id(input: &[u8]) -> (result: Option<(EventId, &[u8])>)
        ensures
            result.is_some() ==> {
                &&& result.unwrap().0.as_bytes().len() == 32
                &&& result.unwrap().1.len() == input.len() - 32
            },
            result.is_none() ==> input.len() < 32,
    {
        if input.len() < 32 {
            None
        } else {
            let (id_bytes, rest) = input.split_at(32);
            Some((EventId::from_bytes(id_bytes), rest))
        }
    }
}
```

### Phase 2: Core Identity Events (Months)

Verify the 12 identity events that establish trust:

```
network, invite_genesis, invite, user, peer, peer_shared,
admin_genesis, admin, user_removed, peer_removed,
invite_accepted, peer_name_update
```

Key properties:
- Trust chain validity
- Authorization correctness
- No privilege escalation

### Phase 3: Projection Correctness (Weeks)

Prove projectors are:
- Deterministic (same input → same output)
- Idempotent (re-projection is no-op)
- Dependency-respecting (only project when deps satisfied)

```rust
verus! {
    // Determinism
    proof fn projection_deterministic(e: Event, ctx: Context)
        ensures
            e.project(&ctx) == e.project(&ctx)
    {}

    // Idempotence
    proof fn projection_idempotent(e: Event, ctx1: Context, ctx2: Context)
        requires
            ctx2 == ctx1.apply_writes(e.project(&ctx1)),
        ensures
            e.project(&ctx2) == e.project(&ctx1)
    {}
}
```

### Phase 4: Content Events (Optional)

Message, channel, group events are lower risk once trust is established. Verify if time permits.

## Cross-Platform Deployment

### UniFFI Bindings

```rust
// In src/lib.rs
uniffi::setup_scaffolding!();

#[derive(uniffi::Record)]
pub struct MessageEvent {
    pub event_id: String,
    pub channel_id: String,
    pub content: String,
    pub created_at: u64,
}

#[derive(uniffi::Object)]
pub struct Protocol {
    // Internal state
}

#[uniffi::export]
impl Protocol {
    #[uniffi::constructor]
    pub fn new(network_id: String) -> Self { ... }

    pub fn create_message(&self, channel_id: String, content: String) -> MessageEvent { ... }

    pub fn receive_event(&self, bytes: Vec<u8>) -> Result<(), ProtocolError> { ... }
}
```

### Generated Bindings

**Swift (iOS):**
```swift
let protocol = Protocol(networkId: "...")
let message = protocol.createMessage(channelId: "...", content: "Hello")
```

**Kotlin (Android):**
```kotlin
val protocol = Protocol("...")
val message = protocol.createMessage("...", "Hello")
```

### Build Pipeline

```bash
# Build Rust core
cargo build --release --target aarch64-apple-ios
cargo build --release --target aarch64-linux-android
cargo build --release --target x86_64-unknown-linux-gnu

# Generate bindings
cargo run --bin uniffi-bindgen generate \
    --library target/release/libprotocol.so \
    --language swift --out-dir bindings/swift

cargo run --bin uniffi-bindgen generate \
    --library target/release/libprotocol.so \
    --language kotlin --out-dir bindings/kotlin
```

## Security Analysis

### Attack Surface Layers

```
┌──────────────────────────────────────────────────────────┐
│                   Untrusted Network                       │
└─────────────────────────┬────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────┐
│            nom Parser (Verified)                          │
│  Guarantees: Only accepts valid grammar                   │
│  Eliminates: Buffer overflows, malformed input bugs       │
└─────────────────────────┬────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────┐
│         Signature Verification (ring/HACL*)               │
│  Guarantees: Crypto primitives are sound                  │
│  Eliminates: Forgery, tampering                           │
└─────────────────────────┬────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────┐
│      State Machine (Verus Verified)                       │
│  Guarantees: Only valid transitions occur                 │
│  Eliminates: Auth bypass, invalid state, privilege escal. │
└─────────────────────────┬────────────────────────────────┘
                          ▼
┌──────────────────────────────────────────────────────────┐
│                   Valid Application State                 │
└──────────────────────────────────────────────────────────┘
```

### What's Proven vs Trusted

| Component | Status | Trust Assumption |
|-----------|--------|------------------|
| Parser | Verus verified | Verus + Z3 correct |
| State machine | Verus verified | Verus + Z3 correct |
| Crypto | Audited (ring) or Verified (HACL*) | Primitives sound |
| Rust compiler | Trusted | rustc correct |
| OS/Hardware | Trusted | No backdoors |

### Remaining Risks

1. **Specification errors:** Proving the wrong thing
2. **Side channels:** Timing, power analysis (not addressed by Verus)
3. **Concurrency bugs:** Limited Verus support
4. **Supply chain:** Dependencies, build tools

## Implementation Roadmap

### Milestone 1: Foundation (1-2 months)
- [ ] Set up Rust project with Verus
- [ ] Define event types (split genesis/ongoing)
- [ ] Implement nom parsers for wire format
- [ ] Basic property tests

### Milestone 2: Core Protocol (2-3 months)
- [ ] Implement and verify identity events
- [ ] Implement and verify trust chain
- [ ] Implement and verify authorization
- [ ] Integration tests against Python prototype

### Milestone 3: Full Event Set (1-2 months)
- [ ] Implement remaining event types
- [ ] Verify projection correctness
- [ ] Sync protocol implementation

### Milestone 4: Platform Integration (1-2 months)
- [ ] UniFFI bindings
- [ ] iOS integration
- [ ] Android integration
- [ ] Desktop integration

### Milestone 5: Hardening (Ongoing)
- [ ] Security audit
- [ ] Fuzz testing
- [ ] Performance optimization
- [ ] HACL* integration (optional)

## LLM-Assisted Development

### Effective Workflow

1. **Human:** Identify critical invariants from TLA+ spec
2. **LLM:** Draft Verus types and signatures
3. **LLM:** Port implementation from Python prototype
4. **LLM:** Attempt proofs, flag what it can't solve
5. **Human:** Solve hard lemmas, identify missing invariants
6. **Iterate**

### LLM Strengths
- Translating between formal languages (TLA+ → Verus)
- Boilerplate code generation
- Common proof patterns
- Test case generation

### LLM Weaknesses
- Novel invariants (needs human insight)
- Deep proofs (gets stuck)
- Security reasoning (misses attack vectors)
- Knowing when it's wrong

### Practical Tips
- Provide TLA+ spec as context
- Ask for drafts, not final versions
- Verify LLM output manually
- Use property-based testing to catch LLM errors

## References

- [Verus Documentation](https://verus-lang.github.io/verus/guide/)
- [nom Parser Combinators](https://docs.rs/nom/latest/nom/)
- [UniFFI User Guide](https://mozilla.github.io/uniffi-rs/)
- [HACL* Library](https://hacl-star.github.io/)
- [ring Crypto Library](https://briansmith.org/rustdoc/ring/)
- [Langsec.org](http://langsec.org/)
- [Project Everest](https://project-everest.github.io/)
