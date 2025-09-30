Consolidated observations:

- LLM made the same mistake of mixing core and protocols again
- You need to build complete scenario tests (sequences of API calls) as early as possible for realistic flows and no artificial data seeding.
- It makes sense to start building local-only scenarios first before building two-peer scenarios
- It is important to consider early how multiple networks exist, and how multiple peers could be on the same network, and how a single ES must express all of this
- It's worth defining a network simulator early to not have fake network separation 
- demo.py might be a distraction if we have a very real scenario test
- make the event store a protocol-specific thing because it involves ID and query gating

Ways to challenge the design:

- Add files but in the less-efficient way where blocks are still signed, to test the file storage efficiency part without worrying about signature inefficiency (test if special casing is needed)
- Make a bittorrent-like UI using our proposed file transfer scheme (perf for large-ish files!)
- Make a real holepunching system where non-NATed peers help NATed peers connect.
- maybe: make a bittorrent-like protocol under the hood (blocks, hashes, give/get, etc) 
- Make a todo-mvc or Trello
- Make something like Telegram channels, or shared ownership of a Twitter feed, community can post comments, peers participating in two communtiies and acting as the bridge between them.
- Tables at an event. Proposals, post its, votes, approval voting. 
- Replicate the reddit moderation interface
- closed-world twitter...  AND with features for posting as a team? They would follow others and have their mentions and would decide together what to repost, and if to reword. tweeting, debating what to tweet, working through DMs, adding someone to the group, removing someone from the group, etc. 
- Vetting new community members with an application form 
- ban together flow. Curate ban lists for others to use. 
- Real time, performance, permissioning

Todo: when we add removal, we need to check if removed as a last step in the pipeline, before projecting, and all removals must go in a queue that is processed serially by the remove checker, which also purges things. 

having commands use a pipeline instead of generating right off the bat is challenging:
1. they often need to provide an id in a response (easy solution: return id's)
2. they often need to provide query results in a response (easy solution: let them specify a query and run it after projecting all envelopes, and return that)
3. they often need to make multiple events that reference each other. (maybe: use placeholder refs that get filled in by update deps in the looping pipeline process???'')'

- ended up using placeholder deps and resolve_deps to fill them in as the events got signed and encrypted and stored
- with user events there was the awkward issue that we didn't have the invite event yet, so we couldn't test that our join operation was successful. since we are assuming that invite links are valid I exempted us from requiring the invite event in the self-signed case, but that's a bit weird and could be refined. maybe not validating until real join is the right answer.
- we couldn't run the whole pipeline and test that it filled in correctly without having a test that actually checked the api results (and ran the whole pipeline loop to get them) so I made command tests check the API results too. this makes them more complicated but is necessary to capture the functionality and closer to a real integration test. we can still have the simpler envelope-only tests
- some commands can only be tested well with scenario tests if we want to run them through the whole pipeline because we need to make tests earlier. 

decisions: we keep protocol/quiet/jobs.yaml. we don't have protocol/quiet/jobs and we extend commands to be able to do a query first and then operate on the query. then we make queries that the sync-request job and the sync-request event projector can use. this is also a bit weird. we'll have to modify projectors to be able to emit envelopes other than deltas (outgoing ones.)  

better decision: no jobs.yaml. jobs handler. reflector handler. both can query and emit envelopes. jobs are triggered by time and provided state persisted by the job handler (e.g. for bloom window). reflectors are triggered by events and we don't need state for those yet but maybe we will.  

infinite loops are a problem with our current event bus system.
but it's easy to add some protection against it and limit to 100 tries e.g. -- did this and it should now emit a runtime error.

- we decided we'd focus on closed-world twitter with team-managed accounts
- and audio/video screen sharing with annotations

we might want to flip the dependency and make group one of the refs of a network event, so that a network can name a main group so it's unambiguous. other groups could name network-id's. i should think about this because it's sensitive. 

there's some ambiguity about what to do with id's for identity events because they're an exception, because they aren't encrypted or shared. and should we store them in the identity table or the event store. 
bootstrapping these things is difficult to reason about and always a bit of a puzzle. it would be nice if the framework was very opinionated and provided that itself.

what's the relationship between identity, peer, user, and network.

for the first user, it sort of makes sense for the identity and peer and user and first group to be created first, and mentioned in network. that way, once you have the network id, you know what the first group is. and you can validate the user just based on them being included in the network. we could create an admin group too and include that in network as well. then there's a symmetry with joining where the user event refers to invite and network and you can validate that it has all the things. it also creates more symmetry with creating and joining a network, except that the user is created when joining. 

i should really understand resolve_deps for placeholder deps and how that works.

Auth is a tangle and it's good to proceed from a rock-solid document. 

i'm really not convinced about where to put identities. 
should pubkey get resolved as a dependency so that sign just has the pk in the envelope?
shoudl we go back to having an identity event like any other with its own table and we just don't share it and exempt it from usual checks?
should identity be a handler and create identity is an envelope that gets emitted by a command, and then filled in by the handler?

getting the TUI demo working is proving to be inefficient. but getting the CLI version working is much easier, because LLMs can see what they're doing, and because we're not managing state in the same way. 
there might be some value in getting a web demo working so that we can test that the API is comfortable and realistic
one intuition i have is to push on the CLI demo so that we have a perfect repl for testing things.
we shouldn't use names as identifiers because the API doesn't. we should use id's. commands like /switch or /group or /channel should show me a set of numbered choices (sorted by created_at_ms) and I pick one
this way you can guess at the choice in CLI with a number
we should have a $last-created shortcut for the invite command where you can just use the last created invite.
/state should show the state of all panels (e.g. which channel is active, which user, which network, etc.) and then /views could show a text illustration of all panels. 
being able to see a log of how envelopes flow through handlers would be interesting

maybe i should focus directly on the commands first and not worry about the demo as much yet. 
i don't like how pipeline runner has tons of code in it for resolving placeholder id's. this should fit into resolve_deps exclusively. maybe the issue is they don't have an event_id yet?

maybe there's a better way of chaining the creation of events. 

TODO: if handler loading order matters at all, that's bad, because it could be OS or timing specific. handler loading order shouldn't matter in the current formulation. We should make ordering deterministic, arbitrary.
TODO: standardize naming of fields on all event types -- it might be good to centralize event/envelope creation somewhere so events only think about their own bespoke fields and list their deps.

i think part of the problem here is that we are cramming together our pure
params=>envelopes logic with our "chain and present to API" logic. one thing we could
do is add an api.py or api_ops.py for each event and leave commands to be purely for
functions that consume params and emit envelopes. then we can do some mumbo jumbo on
the core side so that devs can write simple chains in api.py that create events, get
id's, create more events with those id's, return a query, etc. in the most dead-simple
way where everything is encapsulated. there also seems like something similar going
on with jobs and reflectors, where we need to run a query and emit envelopes in
the jobs case, or we need to receive an envelope, run a query, and emit envelopes
in the reflectors case. are these cases so different? could we combine them? e.g.
sync-request would have a command that takes params (dest_ip, transit_key, etc) and
events and adds the correct data to outgoing envelopes. and a job could query the
window for id's, apply the bloom, query for events, and pass those to the command,
perhaps one by one. let's consider this!

there's an intuition gnawing at me that flows could be simpler. like you could emit an envelope that said what its chain was, or what its relationship was to future envelopes. buuut. that's probably harder to express. 

will we have any commands that do multiple things, beyond what is in flows? or will they all be "create" now? if so, maybe we should just call commands "create".

create seems very light for a cateogry of thing. we could just use a protocol-provided emit_envelope function within 

the idea of chaining together things that use the pipeline and get an id or final envelope from it, and can query, for things where we don't really care about concurrency, is really useful. it lets us have this opinionated pipeline thing for organizing the way stuff gets made and validated and keeping that under control, while also having a way to just write normal programs for things. reflectors, jobs, and commands all ended up being this: "flows". I like the name flows but we could just call them commands to keep with CQRS. (though I think ours do a bit more because they can query too.)   

referring to the key by the event_id and not the hash doesn't seem like the way for key-id because most people won't have the original event_id. 
maybe there is an original event_id with the key hash in it that all the wrapped keys refer back to? 
but it is awkward for group keys. for transit keys and prekeys and peer's it still feels natural. 
the business logic part of resolve_deps is still what we need so that part does make sense

do we need a different thing called transit_secret or is key enough?    

there's some ambiguity around how we map transit key and network, and make sure we're getting network from the transit key used. 
it would be nice to not use/allow network_id in events at all and only infer this from transit_secret_id, but there might be some reason why it's better to simply check that they match
then the question is: where do we do this check or inference? it can be state that crypto.py manages in its own sql db. i don't think we want to project it and do queries because it's sensitive.
should it be in validator? (but i think it's closer to crypto, which can collect this on the way out) 
sync_request could also be writing to a mapping of it when requests are created. this is like projection and make some sense. but then we have to use stuff.
maybe sync_request should get sync_response events back, and then emit the "real" event they wrap for other steps in the process. then all of this would be located in sync. 
at that point we're back to something closer to poc3

resolve_deps could go one more level deep for some things

alishah said this sounds more like a state machine than a pipeline. i wonder what it would look like to be more explicit about states instead of using filters/shapes, and make a list of the potential transitions. 

maybe i should get it done and then focus on how removal / FS is going to work.

maybe i should get hands on with messaging and groups in the demo to make sure it's really doing all the things, and then move forward.

i'm getting some doubts about using resolve_deps to find local secrets instead of making the crypto handler be able to look up local secrets directly. But I think it makes sense to build the thing first to see that better. 

it may be simpler to join by the joiner sending sync_requests, and the other party sending sync requests back, if sync_requests can include address and we aren't checking addresses for known user status. 

if you're on the same network under two identities in the app, in this design, it will be fairly obvious to other peers based on the operations you run and when (like rekey) but that would be true anyway with online status etc. 
generally, reasoning about multiple accounts sharing the same database is a bit difficult.

So I have two options: have separate data and pipelines for multiple peers in the same network or even linked user but in the same client/device. It is nice to have deterministic restore of a whole app state from a single event source, and to model new peer creation as an event for testing purposes etc. However it seems weird to create a separate "I own it" event for every event we receive in the normal case where we are only a memeber of each network *once*. Maybe we only turn this on if we happen to join the same network twice? but at that point is it too late? we could create a "all un-owned events belong to peer-id" event and then mark all future events as owned, e.g., so that we aren't wasting the effort most of the time but are prepared to.  
It seems like this "i-own-it" event thing is the way to go, since otherwise we're really running n separate pipelines, or doing a lot of work to keep them from clobbering each other. This actually could be a really powerful simplification even though it seems complex. 

to make sure i really have this down we should introduce a new kind of test, a replay test, that does a scenario, throws away everything except the event_source, re-runs the event-source through the pipeline, and ends up at the same thing.

obvious things:
- i should draw a state machine diagram
- three important aspects: event type, event state, dependencies resolved

other misc notes:
- might consider a match statement for filter cases https://peps.python.org/pep-0636/
- when i match i should do the thing instead of having the filter be separate from the match. so all of the handlers would get combined into some giant switch


how decrypt could work:
- keeps a table of key hash (local event id) => key
- if the prefix of event data matches key hash, decrypt 
- do it multiple times if needed (transit-layer, event-layer) if needed
- return envelope with canonical event and plaintext event
- or some nested data structure like ciphertext,key(ciphertext, key(plaintext))?

how resolve deps could work:
- keeps table of all validated event_id's
- blocks if any plaintext refs missing
- includes all canonical (encrypted) events as deps
- encrypted deps in envelope get decrypted by decrypt

events shouldn't even have to label deps because they have their own types already. should be able to just include them as an arrray. 

does this make encryption and resolve deps neutral enough to put into core?

say it did: project (deltas=>sql), crypto, event_store, resolve_deps, reflect, remove, and network ops could all be part of core i think.

more important question: can you get linear pipelines for incoming, outgoing, and locally-create.

incoming: decrypt=>resolve_deps (runs pipeline on deps too to decrypt etc) => validate => (reflect) => generate "received at/by event", store, project
outgoing: plaintext + destination + keys + signer => cipher&keyid(signed(plaintext)) + destination => send
creating: plaintext => signed(plaintext) => validate => encrypt => store and generate "created at/by event" => project

do i still need handler/job.sql? project.sql?

Simplifying Local-only Events:

- `peer_local` (local-only version of `peer`, what we previously called "identity")
- `secret_local` / (local-only version of `secret_sealed`, could be unsealed or locally created, can contain any convenient props e.g. `group_id` or `created_at` but all `secret_sealed` must unseal deterministically to create their corresponding `secret_local` event, and all ciphertext must be prefixed with `secret_local_id` (hash of secret_local). Previously called `key`)
- `created_local` an event declaring the creation of another event by a `peer_id` (creator_peer_id, event_id)
- `received_local` an event declaring the receipt of another event by a `peer_id` (receiver_peer_id, event_id, received_at)

Local-only events are not signed. 

Deps in envelopes are just arrays of event_id's and do not need to include type or other information. (All event properties are part of the resolved event.)

Self-created events declare `peer_local` as a dep; when resolved it is available for signing handler.
Signed events declare `peer` as a dep; when resolved it is available for signing handler. 
Encrypted events have `secret_local_id` as a dep, which is resolved directly.

Also, received `secret_local` does not need to be in the Event Source because `secret_sealed` (+ its `received_local`) + `peer_local` (+ its `created_local`) will unseal the key. But if `secret_local` is stored in the Event Source, that's fine; it will dedupe if re-created.

Notes on latest work:
- Does `seen_local` need `network_id`? isn't `peer_id` enough since we aren't letting a `peer_local` exist on multiple networks?
- in our plan document, this line should be: "For encrypted canonical events, event_blob includes a `secret_local_id` (most events) OR `peer_local_id` (for `secret_sealed` and some others) as a key hint.
- i don't get the need for canonical_index.sql -- explain? what's a better way to name it or organize it if we really need it.
- in key.sql, this seems wrong to me: 

```sql
-- Local-only storage of unsealed symmetric key material
CREATE TABLE IF NOT EXISTS key_secrets (
    key_id TEXT PRIMARY KEY,
    unsealed_secret BLOB NOT NULL,
    group_id TEXT,
    created_at INTEGER
);
```
... instead of this approach, why don't we just emit `secret_local` events whenever we unseal a new key (a key reflector?) and let resolve_deps look up secret_local_id, either from the hint or from the plaintext event that is using it and wants to encrypt to it?

...they key/projector.py stuff is wrong. it should emit a new secret_local event not do a projection, 

in key projector we do not need key metadata. i don't think we need a key_projector. groups or channels can keep track of their preferred keys.

- in sync_request/queries, get_user_in_network should be handled by validator, not a query
- sync_request events shouldn't be saved so we shouldn't have to worry about loops.
- don't look up since last sync, just send everything in response to each sync, for now. remember we have a packet drop rate and free deduping. later we will use a bloom. 

This seems wrong. Why not use secret_local_id everywhere instead of key_id? Then it can have a normal canonical event id (hash) like everything else. 
```
# Use key_id as secret_local_id for now (deterministic identifier)
secret_local_id = event.get('key_id') or envelope.get('key_id')
if not secret_local_id:
    return True, []
```

we should remove 'group_id' (multiple groups could use key potentially by mistake and then what) and secret_local_id (should be hash) from secret_local

delete canonical index because we do not need events for network n, just events for peer p.

don't bypass validate for sync_request. put the validation in the right place. use deps to see if the user is valid, and then you're good. (later our remove checker can make sure the user wasn't removed but we'll get to that.)

note: they key reflector is interesting where it's an event that emits another event. and we're doing that for "seen" too.

event store doesn't store after validation because validation doesn't happen until we have plaintext events and we need to store encrypted events we don't have keys for yet (or never will e.g. others' DMs). event_store stores after transit decryption, i.e. once we know which peer it is for. 

why do we need stored_at in event store? we already have seen events that record this separately. 

resolve_deps is a mess. why are we declaring minimal deps if missing? let's simplify this down to, it ensures the array of deps is complete relative to deps in plaintext event (deps are untyped, just a list of event_id) and it takes existing deps (like peer_local_id for locally created envelopes, used for signing later) in the envelope as given for created events, and it looks up the event for every event in deps.  

Oh yeah I messed up. We always seel to prekeys not peer. So event_id maybe does work as the only kind of hint, and we can stop worrying about in hint. Let's think more about this. 

I think resolve_deps filter should work like: grabs all events with deps_resolved: false, only emits events with deps_resolved: true, and almost all other handlers reset deps_resolved to false. maybe some leave it. 

we now have something like an "outer" type for canonical events, 01, ciphertext or 02, sealed. it might make more sense to think of this as a real type that gets stored and emits the plaintext event for further processing than to think of it as something special. 

having a rule that handlers only touch their own tables, if they have any, seems useful.
and that handlers avoid having tables and try to use deps for state as much as possible

it was a mistake to merge transit encryption and event encryption into the same handler. transit should definitely be separate. 

another way to do crypto is to make it a type and a reflector. raw/transit would be a type, and would reflect event-encrypted or event-sealed, and event-encrypted would get stored with seen_local and reflect event-plaintext. maybe what i think of as events now are just special things for event-plaintext and they have a plaintext-type? which other handlers should be reflectors? project can be a reflector (projection => deltas). we'd still want a pipeline to run through these all the way so that we'd have flows and could e.g. get an id of a channel and then use it in the next step of the flow. how does getting id's work now? i think would just be about running the whole pipeline setup inside a context and spitting out created type/id pairs, and we could limit it to "must create just one id at a time" 

seen_local_id is a hash of peer and event_id. this is an important thing to keep track of.

we may want to disable has_seen lookup for perf reasons when there are no duplicate networks?

having special id's for local events makes things messier.
so does having some event types like peer that are not group encrypted, since then they have different id's. 

there's a concurrency problem around projection and event storage if a single pipeline run isn't an atomic transaction, since if events are stored before being projected, and the pipeline is interrupted before projection, events will be lost. 

maybe each event type should express how it is id'ed and id should be a handler, since there is nuance here. 

there's a problem of where to specify that some API endpoints are gated on knowing the peer, and others (like peer.create_as_user) cannot require this gating because we are creating one. 

there's another id problem where in the validate.py handler we validate a self-created event after signing, but before encryption, so we don't have an event id at this point!

can we query for identities without a peer_id or does that run into gating problems too? 

with outgoing events i'm being sloppy about dependencies because really a transit secret or prekey or destination address isn't a dependency. In this sense it would make more sense if an outgoing event was an event that wrapped another event, because then there would just be event and deps, no envelope.  

What are some ideas for simplifying envelopes?

- instead of a ton of booleans, try to have a single status
- still have a boolean for resolve_deps
- have an outgoing handler that keeps maps of peer<=>address, peer<=>transit_key_secret (from outgoing), and group<=>key_secret so you only put `to: peer_id` in the envelope and it figures out the rest.
- have a sync handler that keeps all of these and sends sync_responses directly to the network
- it can contain sync_auth, sync_latest, sync_blob or these can be event types with sync.py files 
- don't store type in the envelope just get it from event plaintext
- we don't need network_id in envelope if transit_unwrap never emits events whose network doesn't match transit key (network_gate enforces consistency)
- do we need missing_deps_list? isn't this stored in a table?
- is there any time we need both ciphertext and plaintext? can we store the event in its current form?
- could we only include plaintext in the envelope and have methods that get you ciphertext and transit_encrypted?
- todo: update secret_id to key_secret_id

if you have a single step for encrypting, storing, wrapping, you can rip the inner/outer key hints off the plaintext and encrypt them each, and add them on in the reverse direction, along with network. you can also rip send_to=address off the event before encrypting it and send. encrypt_and_send(plaintext_event), receive_and_decrypt(raw) => plaintext_event

or 

what if events specified inner_key, outer_key and encrypt will either encrypt or seal, and after it creates inner it writes it to ES as a blob of (inner_key_id, ciphertext_without_inner_outer) and then it creates outer too, and in the reverse direction  

todo: we need to make sure local-only events like peer_secret can't get foisted on us by other users

the graph of handlers is annoying to fix when there are issues. 

one thing i don't like about this design is that in some places we want to make sure there is a bottleneck for security or debugginng reasons, like for network gate or id assign, but instead id's can get assigned by any handler if it creeps in. 

creation flow branches so that the plaintext gets validated and projected and the blob gets id'ed and 
  stored. the projection needs the id so creation should probably be validate, sign, encrypt, id assign, 
  project (we still have plaintext in envelope, and now we have id too)


So this flow would let you have express-style chains of handlers:

receive: network_data → receive_from_network → transit_unwrap → network_gate → store_event
create: flow_request → validate → signature → event_encrypt → store_event
(flows converge)
store: store_event → resolve_deps(decrypt) → event_decrypt → resolve_deps(plaintext) → validate → reflect → project → response

send: not really a pipeline, just set outgoing_event_ids = some_flow(dest_and_transit_params) to create and store the event, then ~ map(outgoing_event_ids).send(outgoing_id, dest_and_transit_params). (Sending is rare and mostly done by sync_request jobs and reflector. Only exception is bootstrap.)

And a refinement would be for no writes to happen in the pipeline, just actions (like block, store, project, send) written to a queue. Then you'd have queues around the pipeline (received, blocked before and actions/writes after). you could have something that processs all the writes. send is a bit weird because sometimes it's triggered by a job and sometimes by receiving an event. 

Another nice thing for reasoning would be to have the dependency resolver act on its own separately from the pipeline. It would need to be able to decrypt things and the crypto operations would be redundant, and the I/O would be redundant, but it would simplify things a lot to know that a) the depedency resolution never itself leads to writes. this way you have one incoming queue. 

Something like:

next(env) to continue and 
dispatch(type: 'ACTION', payload) to dispatch effects

Then the question is whether block/unblock are first class framework features or protocol-specific instead. The argument for making them first class is that they're tricky to get right so having a stable block/unblock would be really useful. I'm going to continue with it not being first class because I want to understand it alongside other pieces and I think it will be easier if it's one of our effect reducers. 

loop:

0. queue for pipeline
1. run pipeline on queue item (reading from ES when needed) and drop or dispatch effects
2. write effects (store, queues, projections) -> back to queue and pipeline

loop:
1. run all jobs 
- sync_request
- unblock_deps (pulls off newly_validated, blocked, enqueues to unblocked. has a lock on db)

Making this an external core-level thing might make more sense. All it needs to operate on are id's, which can be anything. resolve_deps is still what accesses the event store. This gadget just keeps a list of blocked, blocked-by and re-queues unblocked things. 

The next question is how removal would work. 

So say I have a "should_remove" handler in the design. It removes all new incoming stuff, fine. But I need to be able to emit a remove operation with no concurrent writes. Or I need a job that's constantly checking. ***The key thing is it needs to throw away all other concurrent pipeline operations and make them re-run. (Also, shouldn't this be easy with an ES, i.e. just replay?)** As soon as a removal is valid, you have to do that. And then whatever deletes it applies are all-inclusive, because when the pipeline restarts the remove handler will have access to the new removal event and do the correct thing. Does blocked work this way?


Does blocking work this way? Yes, it's similar.

One approach would be to do a compare and set, where we remember the latest event at the start of the pipeline, then check all new events since the start of the pipeline for either being deleted (in the deletion case) or being unblocked by an event we just validated, or unblocking an event we just blocked (in the unblocking case). Other events that do not fit this criteria don't matter. Resolve deps has two jobs: only emit resolved (easy) and emit all resolved ASAP (hard) https://stackoverflow.com/questions/52309428/event-store-and-optimistic-concurrency

"Typically this is implemented by passing the version you expect the data to be at to the database server. Only when the version matches the current version the update will go through successfully." https://stackoverflow.com/questions/26975596/implementing-an-event-store-with-optimistic-concurrency-checks-over-multiple-cli

So the pipeline run could handle unblocking itself without any issues provided that it reads the versions of the data it's reading from and then fails to do anything (including update the queues it's pulling from) if that data changes before commiting. (E-Tags)

https://youtu.be/LGjRfgsumPk?t=769 -- when can you use a query in a command? -- you can use this approach whenever it's okay if you're acting based on a read from some time ago. If you write the new event and say what version of reality you were using when you did that, you can go and figure out later if you messed up. If your read model gives you a version number based on its own understanding of where it is for every query, then when you write based on that, if you mess up some concurrency detail in general or in the moment, you can go back and fix that!Then later when the read model gets the event it can spot the mistake!! :) 

So for deletion your "delete" event could get written like "And I have deleted all events up to VERSION" and then a future operation could check if there were any events you didn't delete between VERSION and your delete event? My hunch is that you could do this with "validated" too.  

Khan's algorithm for topological sorting seems to be the relevant reference: https://en.wikipedia.org/wiki/Topological_sorting
https://grok.com/share/bGVnYWN5LWNvcHk%3D_144fe20d-feda-4bd6-9b22-86154b83f111

Both unblocking and deletion come back to keeping the pipeline run (or a batch of many pipeline runs) in a single-threaded background process in a single transaction.

Now the thing is that it's a little harder to test if these steps depend on the database, but there's not really any way around that. 

important atomic bits of the pipeline:
- on receive: once it is stored its id is queued (it can be lost before being stored and that's ok) 
- on create: don't return an id to a flow until after storage (and rule 1 holds)
- on store: 
 - re-enqueue all items in the missing-keys list that match keys we have, be readable to all subsequent  

one question is: I should at least consider the atomicity-chill approach of a GC sweep instead of something atomicity sensitive. and it's boundable because I know the `received_at_ms` of each event. 
- this seems like it would be plenty fast enough. 
- does any new event since I last ran (plus grace period) unblock/decrypt/delete any older events?
- i can use jobs for this, since it's okay if my queries are a little out of date as long as the grace period covers this. \
- this is a superior approach because it does not require reasoning about concurrency. 
- the pipelines themselves can be expressed as jobs if i want to do that. 
- you provide the framework with a `receive` job and it provides you with a `send()` function. 
- the job needs a lock because overlapping jobs creates ambiguity around "last ran"