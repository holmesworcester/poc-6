# Device Linking Causal Analysis

## Expected Causal Chain (from spec)

From `docs/quiet-protocol-specification.md` lines 441-448:

**Device Linking (new peer for existing user)**:
```
[REMOTE] network ← FORCE VALID on accept (trust anchor stored in trust_anchors)
  └─► [REMOTE] admin_grant
        └─► [REMOTE] D1_peer_shared
              └─► [REMOTE] invite(mode=peer) (signed by D1_peer_shared)
                    └─► [LOCAL] D2_peer_shared (signed by peer invite) ← BLOCKS until invite arrives
```

**Critical insight from spec (line 450)**:
> "The `invite_accepted` event for ANY invite type (user or peer) must include `network_id`
> so the network can be recorded as trust anchor."

## Datalog Representation

```datalog
% === TRUST ANCHORS (force-valid) ===
valid(D1, network) :- created_locally(D1, network).
valid(D2, network) :- invite_accepted(D2, _, network_id).  % trust anchor

% === D1's CHAIN (network creator) ===
valid(D1, admin_grant)    :- valid(D1, network), signed_by(admin_grant, network).
valid(D1, user_invite)    :- valid(D1, network), signed_by(user_invite, network).
valid(D1, user)           :- valid(D1, user_invite), signed_by(user, user_invite).
valid(D1, peer_invite)    :- valid(D1, user), signed_by(peer_invite, user).
valid(D1, D1_peer_shared) :- valid(D1, peer_invite), signed_by(D1_peer_shared, peer_invite).
valid(D1, link_invite)    :- valid(D1, D1_peer_shared), signed_by(link_invite, D1_peer_shared).

% === D2's LOCAL EVENTS ===
% D2 creates its peer_shared which depends on the link_invite
valid(D2, D2_peer_shared) :- valid(D2, link_invite), signed_by(D2_peer_shared, link_invite).

% === SYNC: D2 MUST RECEIVE D1's EVENTS ===
% For D2 to validate link_invite, it needs D1_peer_shared
valid(D2, link_invite)    :- synced_to(D2, link_invite), valid(D2, D1_peer_shared).
valid(D2, D1_peer_shared) :- synced_to(D2, D1_peer_shared), valid(D2, peer_invite).
valid(D2, peer_invite)    :- synced_to(D2, peer_invite), valid(D2, user).
valid(D2, user)           :- synced_to(D2, user), valid(D2, user_invite).
valid(D2, user_invite)    :- synced_to(D2, user_invite), valid(D2, network).
valid(D2, admin_grant)    :- synced_to(D2, admin_grant), valid(D2, network).

% === SYNC REQUIRES CONNECTION ===
synced_to(D2, E) :- connection(D1, D2), shareable(E), valid(D1, E).
connection(D1, D2) :- connection_ack(D1, D2).
connection_ack(D1, D2) :- connection_request(D2, D1), valid(D1, connection_request).
```

## The Chicken-and-Egg Problem

**Q: Can D2 send connection_request before D2_peer_shared is valid?**

The connection_request is encrypted TO D1's connection_pubkey. D2 needs:
1. D1's connection_pubkey (from invite_link - available!)
2. D2's own peer identity to sign the request

**Q: Can D1 process the connection_request before D2_peer_shared is valid FOR D1?**

D1 needs to verify the connection_request signature. If signed by D2_peer_shared,
D1 needs D2_peer_shared to be valid. But D2_peer_shared depends on link_invite,
which D1 already has valid.

## Observed State

```
After 200 ticks:
- invite valid for D2: FALSE
- D2_peer_shared valid for D2: FALSE
- blocked events for D2: 3
- connections for D2: 4 (increasing!)
- incoming events for D2: 0 (!)
```

**Key observation**: D2 has connections but 0 incoming events via sync.

## ROOT CAUSE FOUND

**Bug in `events/network/connection_request.py` function `_get_invite_private_key()`:**

```python
# BUGGY: First query returns ANY pubkey for the peer, not the invite's key
invite_key_row = safedb.query_one(
    "SELECT private_key FROM pubkeys WHERE owner_peer_id = ? AND recorded_by = ? LIMIT 1",
    (peer_id, peer_id)
)
if invite_key_row and invite_key_row['private_key']:
    return invite_key_row['private_key']  # WRONG KEY RETURNED!

# CORRECT: Second query gets the actual invite_private_key
ia_row = safedb.query_one(
    "SELECT invite_private_key FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
    (invite_id, peer_id)
)
```

**Impact:**
1. D2 signs connection_request with WRONG key (TreeKEM pubkey instead of invite key)
2. D1 tries to verify signature using invite_pubkey
3. Verification fails: "invalid signature"
4. connection_request not projected to pending_connection_requests
5. D1 never sends connection_ack
6. No connection established from D1's perspective
7. Sync never happens
8. D2's events stay blocked forever

**Fix:** Remove the first query (lines 147-154) - it's incorrect for both inviter and joiner cases.
