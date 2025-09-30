We start with message_via_tor.md and then expand. 

### Unblocking Blocked Messages

Sometimes a `message` will arrive before we receive its corresponding `peer`. We can handle this by having the `message` make a list of blocked messages and the `peer` projector call the Message Projector on messgaes a new `peer` makes valid. 

### Private Groups

We can add a `key` event and a `sealed-key` envelope that seals to a single public key, and then issue `sealed-key` events for all keys to all peers, to create private groups of `peer`s e.g. for DMs.

### Event-Layer Encryption

We can add another layer of encryption to `peer` and `message` events with a `psk` created (or re-used, if existing) by `invite` and the envelope `encrypted-event` which gets converted into `peer` or `message` by its adapter. the `outgoing` envelope would then require `encrypted-event` for these message types.

### Sync Efficiency

We can make `sync` a bit more efficient by adding a bloom filter and a random, per-event salt to our `sync` request, and modifying `sync.validate` so that it only returns `peer` and `message` events that are negative matches to the bloom, i.e. events the requester does *not* have.

This is not entirely realistic for large numbers of messages but it points in a realistic direction.

### Disappearing Messages

For disappearing messages, can add a `ttl` to `message` and delete expired messages. A `messages` job can delete disappearing messages at the appropriate time.

### Messages With Attachments (Blobs)

We can send attachments like images or videos by splitting them into `slice` events identified by a `blob-id` in a `update-message-attachment` type pointing to the `message-id`. (We can use the hash of the encrypted message event as `message-id`.) The `blob-id` is a hash of the file and verifies it. No events can be larger than 512B so slices must be small. 

### Lazy Loading

To sync the most recent messages, or only sync auth-relevant messages, we add `lazy-sync` with a `cursor` (`message-id`) for pagination.

These trigger the same `sync-response` event as `sync-request` but focus on the latest messages.

### Signing

We can add a `signed-event` envelope that signs events and becomes part of all `encrypted-event`s 

### Removal

We can add a `remove-peer` event that, if present, will cause all events signed by that `peer` to be dropped.

### Proof-of-invitation

Rather than a `psk` that can never change, we can join with a proof of invitation, a signed `user` event from a public key KDF'ed from in an `invite` event, encrypted using a secret we got in the invite-link. (This should be sealed and encrypted.)

### Linking Multiple Devices

Users work on multiple devices, so we introduce a `link-invite` event that works like `invite` but lets users link multiple clients and works just like `invite` but limited to inviting new peers to join *as the same user*. 

(Show how this works)