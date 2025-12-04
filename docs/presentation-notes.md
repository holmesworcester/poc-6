Summary: 

We have: 
- a database
- events that are stored in the database 
- ...and trigger other writes to the database, and sometimes the creation of other events
- commands (in `events/event_name.py` files) that create these events either in isolation or orchestrated combination
- cron-like jobs which run commands on tick and track the last times they were run (in the database) 
- some queues (also in the database)

One of the jobs is a network simulator that moves packets from outgoing to incoming queues adding latency and packet loss optionally.

From our events we build DAGs of content with: 
- deletion and disappearing messages.
- identity and authentication
- groups and encryption with forward secrecy (both transit layer and data layer)

We store events encrypted, because that has to be their canonical form, because we need to be able to refer to things others may not be able to decrypt, like when deleting something.

The tricky part is reasoning about auth and bootstrapping, because the graph is very dense and it's very easy to accidentally create chicken and egg cycles, especially as you add desired features like device lining or encryption with FS. 

Another tricky part is accommodating many different potential accounts on the same device (like slack would have workspaces)

Easy: content, most features
Medium: encryption, dependency resolution
Hard: auth, especially bootstrapping, especially as you add features

One important simplification is to couple as few other features or mechanisms as possible with auth and bootstrapping! E.g. you want your connection and syncing bootstrapping to intersect AS LITTLE AS POSSIBLE with your DAG bootstrapping or you get a rat's nest of chicken-and-egg cyclical dependencies, either at the data layer itself or in time.

We have a prototype CLI that works in both interactive mode and non-interactive mode (so LLMs can "see" it and self-QA it). End-to-end tests are trivial because multi accounts can exist and interact in a single instance. 

- Events get recorded by ("seen by") peers (`recorded_by` is an event too)
- Recorded events get a `recorded_by` event generated for them referencing their event id
- This is subtle and important: it means the same event can be recorded by multiple peers on a device, which is a possible case if we let users have multiple accounts on a single device, and which is helpful for testing "multiplayer" in a single instance. 



Key ideas:
- Everything is an "event"
- Events are small, content-addressed (event id)
- Events synced whole, as packets
- Events describe content, groups, identity, and network state
-- Content: messages, reactions, etc.
-- Identity: network, users, peers (devices / accounts)
-- Groups: group prekeys, group keys, sealed group keys
-- Network: transit prekeys, transit keys, addresses, intros
- Events get recorded by ("seen by") peers (`recorded_by` is an event too)
- Recorded events get a `recorded_by` event generated for them referencing their event id
- This is subtle and important: it means the same event can be recorded by multiple peers on a device, which is a possible case if we let users have multiple accounts on a single device, and which is helpful for testing "multiplayer" in a single instance. 
- It was helpful to limit projectors to only write to their own subjective view, so I introduced an idea of safe and unsafe db. We prefer safedb because it's impossible to write other peers accidentally. This helped keep the LLM on the rails too.   
- recorded_by also lets us have a multi-account setup where all data is expressed in a single event store; there is no other permanent state that cannot be restored by reprocessing the event store
- We don't even have to process it in order or with idempotence: in fact I have tests that deliberately process events out of order and multiple times to make sure the logic is right.
- We have a network simulator gadget that pretends to be a network and pulls from the outgoing queue of one peer and sends to the incoming queue of all peers
- Transit encryption routes to the correct peer (every peer pairing has its own transit_key)
- Transit_keys can be encrypted to prekeys and rotated (with prekeys purged) for transit forward secrecy
- We do the same thing for group forward secrecy
- We have a SQLite database
- We "project" events to turn them into data
- We prefer idempotent, non-destructive operations in SQLite (INSERT OR IGNORE, e.g.) and "picking a winner" on the query side. This is important for things like message updates and reactions. We have a global counter (highest seen + 1)
- We can ensure that events get projected in an order that makes sense by giving events dependencies on other events.
- Whenever an event refs another event id, we (almost always) treat it as a dependency
- Dependencies are resolved with topological sort and Kahn's Algorithm: blocking, keeping a blocked_by count for every unmet dependency, and unblocking when it's zero.
- We use atomic transactions in SQLite to ensure that we aren't blocking and unblocking concurrently.
- Encryption keys are "hinted" with an event id prefix to an encrypted blob. 
- We use event id prefixes (hashes of events with keys in them), not hashes of keys, to be consistent with other refs
- We then treat "blocked by missing key" as a subcase of "blocked by missing dependency"
- For testing, we have "scenario" tests that test the operation functions that a real API would use and can build up specific scenarios easily and confirm the queries are what we expect. This is pretty much "end to end" 
- For prototyping simplicity, we do not build a real API yet, but it would be easy.
- For read-only API activity we use simple SQLite queries
- For complex flows we can combine the creation of multiple events and queries; it's okay to query projections when creating new events, as long as dependencies guarantee they will be made first
- However, it might be simpler to have pure functions for creating and projecting events. I have a branch where I work on this and it feels cleaner and more testable, but I'm still finding the shape of it and the limits. 
- A prototype uses our "API" by calling functions directly
- It's easy to give the frontend nice affordances like returning the latest messages when you send a message, or returning all reactions with the message list
- Account bootstrapping stuff is really hard, but features are easy, even complex ones.
- We have a CLI prototype that is both interactive (for us) and non-interactive (for the LLM, and for easy testing)
- We can tell the LLM to play with the prototype and self-QA it, and it does a good job. Then we can immortalize these user journeys as scenario tests
- We can have CLI tests too quite easily (multi player is in the same client) but scenario tests are preferred because they are closer to the data / truth
- we have files and file_slice events, also content addressed, and they seem to be performant enough
- our syncing algorithm kind of sucks (coupon collector problem) but it is really simple.
- there's a better way to do sync that I have partially written up that is very efficient (inspired by some academic work used by the Nostr folks) and it's easy to experiment with things
- We have "jobs" that run on every tick or at whatever frequency we want
- Syncing is very easy to simulate, but I haven't gone crazy with making the simulation accurate
- In our scenario tests we have a sync gadget that runs "ticks" (cycles of processing events) and detects which peers are syncing at all, and then ticks until they have parity for certain kinds of events.
- In our CLI prototype we can tell it to tick
- We can also fast forward time to test disappearing messages or forward secrecy



