
Here we want a basic protocol for testing group auth. In this context we assume an eventually consistent multicast broadcast network that all users can join by knowing the `network-id` (a simple case that matches e.g. if the network was secured with a PSK). All users see all messages, so provided we account for the possibility of reordering (partitions) in our tests we can simulate the network as a single set of events and a single api for fetching e.g. messages. all commands that create events simply create the event and project them. there isn't a distinct receiving step, e.g. as in our other protocols where we handle events in an incoming queue. We simulate partitions by adding a permutation test to test-runner and ensuring that all orderings result in the same state. 

We can think of this toy protocol as a test that our groups/auth protocol can behave like a CRDT, without the complexities of message syncing, event encryption, and transit encryption.

- there is a single shared state that all identities can modify by creating and projecting an event in a single transaction.
- all tests attempt all permutations of events to ensure they end in the same projected state.
- all peers create an `identity` keypair but do not share it
- all shared events have a real signature of plaintext 
- all events have an `id`, the hash of the signed plaintext, `group-id`, `message-id`, `user-id` etc are all `id` 
- events often refer to other events by this `id` 
- `network` has a name
- `invite` event has a public key derived from a secret -- invite-link has the full secret and a `group-id` (the first group)
- `user` has a `name` an `invite-id` the `network-id` and `group-id` (of the first group) and is signed with the public key from that invite, using a kdf of the secret, and signed with `peer-id`
- a `user` is also valid if they are signed with the same pk as `network` (this is the first user, and they create this `user` event in network creation)
- a `user` corresponding to a valid `invite` made by an already-valid `user` is a valid user.
- `blocked` is a table in projections where we list blocked `event-id` with blocked-by `event-id`
- if an event names an event we have not seen yet, project it to blocked
- if an event is signed by a non-user, project it to blocked
- all projectors call `blocked.unblock` after every projection on the events they projected, in case they unblock others. `blocked.unblock` passes any event whose blocked-by matches the projected event to handle, for projection.
- `group` event has a `name` and `user-id` (which must match signer, or invalid)
- `add` has a `group-id` and a `user-id` (adds an existing network member to a group)
- `link-invite` has a `user-id` functions like `invite` in other ways: it returns a shareable link with a secret and another peer can use it to be a linked peer of that user (multiple devices for a single peer)
- `link` is like `user` but it associates a `peer` with a `user`. it has a `peer-id` and a `user-id` and must be signed by the pk in `linked-invite` and `peer-id` 
- `channel` has a `name` and `group-id`
- `message` has a `channel-id`, `peer-id`, `user-id` and `text`. `peer-id` must match signature and `user-id` must match `peer-id`. 
- a message in `channel` that is not from a member of `group-id` is hidden  
- some of these checks are duplicated across events and could be rolled into a common helper
