
## Minimal Functional Network

Once we get our framework in place, let's try building a simplified network with the following event types representing user data: `identity`, `peer`, `sync-request`, `message`, `incoming,` `outgoing`

In this simplified p2p network, every user has an identity keypair (`identity`) with its public key (`peer`) as a permanent address, with no need for ports or hole punching, and transit-layer encryption between peers. It isn't so far-fetched: Tor, I2P, and others offer this, and there are private messaging and filesharing apps that really work this way, e.g. Ricochet or OnionShare.

Like Slack (or Tor) a client can have multiple identities, so we can easily test an entire network by having multiple peers in a single client, provided we have a handler to simulate the network: `tor-simulator`.

### Identity and Peer Event Model

**Critical Concept**: Each identity maintains its own view of the world based on what it has received. This is tracked through the `received_by` field in event envelopes.

An **identity** is a keypair (public key + private key) and a name. The public key serves as the permanent address for that identity.

A **peer** event and its envelope represents knowledge that a specific identity has about another participant in the network. (A raw peer event without an envelope simply represents the existence of a peer.) The same peer can exist as multiple events (really, events + envelopes -- the peer event data itself will be the same for any given identity) in the event store with different `received_by` values, representing different identities' knowledge of that peer.

For example:
- Alice creates her identity (pubkey: A, privkey: a)
- Bob creates his identity (pubkey: B, privkey: b)
- When Alice learns about Bob, a peer event is created: `{type: "peer", pubkey: "B", name: "Bob"}` with envelope metadata `received_by: "A"`
- When Bob learns about Alice, a separate peer event is created: `{type: "peer", pubkey: "A", name: "Alice"}` with envelope metadata `received_by: "B"`
- These are stored as two distinct events in the event store, even though they have similar payloads

### The `received_by` Field and Its Consequences

The `received_by` field in the envelope metadata is fundamental to the multi-identity architecture:

1. **Event Ownership**: Every event in the system belongs to a specific identity indicated by `received_by`. This includes:
   - Peer events (who this identity knows about)
   - Message events (messages this identity has sent or received)
   - Sync events (synchronization requests for this identity)

2. **State Segregation**: Each identity's state is derived only from events where `received_by` matches that identity's public key. This ensures:
   - Complete isolation between different identities on the same node
   - Each identity sees only its own peer relationships
   - Messages are visible only to the sending and receiving identities

3. **Event Duplication**: The same logical event (e.g., a message from Alice to Bob) will exist as multiple events in the store:
   - One with `received_by: "A"` (Alice's copy of the sent message)
   - One with `received_by: "B"` (Bob's copy of the received message)
   - This is by design and ensures proper state segregation

4. **Join Process**: When joining via invite:
   - The invitee creates their new identity
   - The invitee stores the inviter as a peer with `received_by` set to the invitee's public key
   - The invitee should attempt to send their identity information (as a peer event) to the inviter
   - The inviter will receive this peer event with `received_by` set to the inviter's public key

5. **Network Simulation**: The `tor-simulator` handler is responsible for:
   - Taking outgoing events from one identity
   - Creating incoming events for the recipient identity
   - Setting the correct `received_by` field on the delivered event

### Handlers

`identity` has:
- `create` (creates an `identity` containing `pubkey, privkey`, and calls `peer.create(privkey)`)
- `list` (provides a list of all client identities, e.g. to API)
- `invite` (returns an invite-link containing this `peer`, for sharing out-of-band)  
- `join` (consumes a valid invite link, calls `create.peer` for the `peer` in `invite`)

`peer` has:
- `projector` (checks that it has a public key, and adds to Projection)
- `create` (creates and Projects a new `peer` event) (NOTE: projection should be automatic for creation)

`message` has:
- `create` (consumes pubkey, message-text, and time_now_ms from tick, creates `message` event, puts it in the `outgoing` envelope with address information and projects to outgoing in state.)
- `projector` (checks has pubkey matching known `peer`, has text, has time, else does nothing or see note.)
- `list` (given a `peer` public key, returns all messages known to that peer with their handles and timestamps, as json, called by API e.g.)

**Important Note on Message Ownership**: The `received_by` field is how we track which messages belong to which identity. Each message has:
- `sender`: The pubkey of who sent the message (same for all copies)
- `received_by`: The pubkey of the identity that owns this copy of the message

When listing messages for an identity, we ONLY show messages where `received_by` matches that identity's pubkey. This ensures:
- Each identity sees only their own copy of messages they sent
- Each identity sees messages they received from others
- Identities don't see copies of messages sent between other peers

Note: a message might arrive before the `peer` event, so it will not have a pubkey matching a known peer. To address these cases, the message projector should mark the messagee as `unknown-peer`, and the API should not show these messages. Then, whenever a `peer` arrives that matches the pubkey of an `unknown-peer` message, the peer projector should remove the `unknown-peer` flag. Another way to do this would be to have the message handler register a listener on new projected peers, check if they match an `unknown-peer` message, and modify the message.   

`sync-peers` has:
- `projector` (assumes that only invitees and members know this pk, calls `outgoing.create` on all `peer` known to that identity)
- `create` (makes a `sync-peers` event given a sender `peer`)
- `send` (calls `outgoing.create` on the `sync-peers` event, given a recipient `peer`)
- a job that every tick, from every `peer` sends `sync-peers
` events to all their known `peer`s.

`tor-simulator` has:
- `deliver` - converts all `outgoing` events to a recipient `peer` to incoming events for that recipient `peer`.
- a job that runs `deliver` on every tick

### Envelopes

`incoming` is just the same as an event because we don't have any origin information and don't care about received at yet.

`outgoing` envelope has address information of recipient.

Imagine an API that has access to these commands. Users create an identity with a peer, invite others or join a new network (the API enforces that they don't invite others and then join) and they send a message, which is stored locally and added to `outgoing`. `tor-simulator` converts all outgoing to incoming, and the recipient peer identifiers on each incoming event ensure routing to the correct identity. `sync-peers` events go out on every tick and when validated by other peers they lead to all `message` and `peer` events being synced (inefficiently!).