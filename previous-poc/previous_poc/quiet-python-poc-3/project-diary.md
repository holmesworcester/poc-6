todo: 
- incoming_handler.json has bad smell
- the way we're using sqlite is weird. i think handle should be using transactions e.g.

Notes:

- I settled on the overall project structure in a previous iteration
- Intentionally creating handlers and test runners as a bottleneck is helpful for LLM wrangling and for following its work
- Having multiple protocols in the same project is nice, to avoid context-shifting between different projects and to make it lower-cost to try new variations
- building a text demo was a good idea in *some* ways: we don't have to mess with ports and a server running, or a browser, and sometimes the llm was able to test a screen just by looking at it.
- nudging the llm to create textui "screenshots" was helpful, but in the end it was best to avoid modals entirely and use /commands
- a good mode for demos is either CLI or, if persistent state is required, "/" commands. 
- we could also add cheap persistence and just use a CLI instead of a GUI at all... perhaps this is an advantage to starting the real sql phase of the project sooner instead of being in-memory only.   
- basic mismatches between the commands and the api spec were costly. in implementing something in the UI i could have it look at the api code and the commands underneath.
- i'm also interested in using types to clamp the api spec to the command signatures
- having `api_results:` in tests as a separate thing will help. 
- it's cool that a lot of the custom testing can be centralized into core and not /protocol and shared across protocols, and that a dummy protocol can be used to test the tests. 
- it might be helpful to declare core off limits when doing a protocol implementation. llm sometimes wants to edit tick. (or change the working directory to protocol) 
- i'm seeing a lot of type issues. 
- dummy crypto makes things easier in some ways and harder in others: bugs can persist for longer. real crypto is kind of good for checking correctness.
- invite and join are a nexus of difficulty
- need to find a good place to remind it about basic things like venv and runner
- it would be good if the demo came with the framework. we could have a cli demo and a gui demo and think about some tricks to get it to autogenerate. the gui demo would be identities and slash commands maybe. or we could make columns customizable for api queries and command entry context, so you get something like a message experience. 
- like lots of boxes and tiles. or you could get a bunch of checkboxes about what queries from the api you want to show and what values you want to put in them based on stuff you have! 
- i have to remind the llm about projecting after a command quite a bit.
- it seems that when we get to blocking/unblocking, there could be a protocol-generic way of doing it: every event has an event-id (hash of signed plaintext event) and there's a blocked-by table that lists the event-id that's blocked and the event-id that's blocking, and whenever we newly project an event we re-handle all the events in blocked. that way there's a first class "mark blocked" function, a first class "get id" function, and everything gets unblocking for free. we have to think about cycles but i don't think there are any because once an event gets projected it's in, and that won't happen again. and we make all this atomic. 
- keeping protocol-specific stuff like identity out of test runner is a real battle with the llm. one instruction could be, we are only working on framework and not protocol, or vice versa.  
- yaml seems more readable than json and it has variables which are good for tests. and you can mess with the format more and add comments and linebreaks etc. probably that's best
- i don't know how to make a closed loop that lets the llm use the demo the way i would 

- when working on the demo a lot of basic non-realistic things show up immediately; ideally we would cover these in handler tests first. 
- it would be nice if projectors didn't have to think about sql or managing transactions and instead just returned some lump of state, but i think they have to query sql no matter what
- there's a question about how to organize sqlite transactions: we use "MANAGE_TRANSACTIONS=True" on jobs that are per-item queue drainers. 
- some comamnds (queue drainers like process_incoming) are mutating state directly instead of just returning events for projection. is this avoidable? what if the projector removed an incoming event from the queue and process_incoming just returned a ton of events for projection?  

some ideas:
- projectors can be pure functions that consume events and return deltas in some arbitrary json dsl `[ {"op": "insert", "table": "users", "data": {...} } ].`
- then you can test that projectors are returning the expected thingyou
- how does this work when consuming an incoming-queue? A. the delta they return has to remove the incoming event from the queue. 
- with these deltas, i can translate them into changes in in-memory dicts in my tests, and in production i can use sql, or test that separately.
- i can use my own API to get json states to assert on
- can i use my own API to create "given" states? this is going to be less readable than just making the states but also it forces them to be real.

One way to do things:
- projectors consume events and query results, return deltas
- commands consume params and query results, return events
- queries consume params and dict or db, return query results  <==this is the only one that has to deal with the DB!
then we would add some check to make sure we could convert from dict to db and back, for every query test. 

Another way to do things:
- sagas listen for events, keep their own state, emit new events (events with all dependencies, decrypted events, validated events)
- projectors listen for validated events, return deltas and projected events
- commands consume params and create events (but these events need to get listened to by sagas!)
- question: how does incoming and outgoing data get dealt with in this model? are those just event types? 


Sagas/Process Managers: For coordination across aggregates, use a saga to listen for dependent events and emit new events (e.g., "MessageDecrypted" with plaintext) when all parts arrive. The projector then handles the new event simply. This keeps individual projectors independent but adds a coordination layer.

--

We could have a generic aggregator that listens to all new events, tracks dependencies, and emits an "event with all dependencies" (an event-id and a list of event-id's) when all the dependencies arrive. Then projectors always have all dependencies. The problem there is that a message projector has to explicitly check if the user is a valid user, and the add is a valid add, etc. So this isn't very useful. We need some listener that is collecting events and emitting valid events. 

So a message handler would do what? 
- listen for messages
- check that the user and channel were valid
- the event would have to include a permission ref to "where i am added to this group" 
- then it would check that all refs were valid. 
- would also check "not deleted" 

An annoying case in this model: it would be hard for commands to know when they had succeeded and return the latest e.g. latest messages in channel. Maybe it's fast enough that we don't have to do that. Maybe commands just return `command` events that consist of their parameters and listeners process those too. 

- handle.py in core needs type in order to work properly, but this is protocol specific event data
- so we had to introduce a requirement that all events have type and id. 
- but projectors create the event store in their own way for each protocol
- the need for deletion is what made me not want the framework to handle the event store

- introducing the scenario tests and a real CLI interface for the TUI demo is powerful. the LLM cooks until all problems are fixed.

- it seems like it would be possible to make a TUI toy with a singleton, in-memory dict (easy mode!) with a real api that just *behaves* the way we want it to (i.e. like a slack) and then build a ton of scenario api tests for it that test all the complex walk throughs you want, and then challenge the llm to swap out the backend with p2p architectures of different kinds. 
- we could give it a network simulator connecting different clients and some tips like "model all user actions as events" and see what it comes up with.
- we could even introduce some tests it has to pass for message encryption and forward secrecy. 
- what's the best way to test crypto? 

- in my current design it would be pretty reasonable to have the framework handle the event store and blocking, and supply dependent valid events to projectors once all dependencies are valid. it could also do signature checking and decrypting event layer encryption.
- are there any events where i'm depending on some state beyond just valid prior events? deletion is one. 

how would i pursue this? e.g.:

1. take the existing design and pull event store into framework
2. projectors can tell the event store that an event is valid and projected
3. framework distinguishes between blocked / unblocked events and unblocks events when valid deps come in
4. framework keeps tombstones for messages, channels, users, peers and just drops them. projectors can add to this list. 
5. standardize on event id as hash of encrypted event and make this owned by the framework. 
6. make type owned by framework so it can send to the right projector
7. make signature check owned by framework? or maybe let us put some middleware in between events and all or almost all projectors to check the signature. 

either way, having every projector worry about signature checking feels like a miss. 

- it seems easy to insert some protocol-specific middleware in handler.py
- somewhere in the protocol would define middleware, what types it applied to, and what order it applied in. we could try to put general functions like missing dependencies, blocking in here. some middleware could run after the projection too, effectively listening for valid projections and acting on that.
- this middleware could supply an event's dependencies for the projector too, so that it didn't have to query the db. I think this helps with concurrency too, i.e. with the problem of an event blocking and the event that unblocks it coming in concurrently, since we'd be potentially centralizing the unblocking role and could give it a lock or lease.

the middleware could tee up a projector in some way so that it is only ever operating on a valid event with its dependencies.
handlers feel like a horizontal spread of functionality, and middleware/pipeline feels like a through line of "how we process things"  

if i want to go a bit farther, the projector could be emitting some op object that a later layer of middleware processes. this does seem like it would be a lot nicer and i should probably try it. 

grok thinks some mix of the sagas or pipeline approach is good. 

incoming_pipeline = [decrypt_transit, check_removal, store, decrypt_event, dep_check, sig_check, validate(type), unblock_deps, create_deltas(type), apply_deltas]
creation_pipeline = [sign_event, sign_check_mw, dep_check_mw, projector, apply_store_unblock_depds]
outgoing_pipeline = [find_event, encrypt_event, encrypt_transit, send] # starts with event-id

[projector, apply, unblock_deps, ]

the next step would be to fork the existing poc-3 and add a pipeline for unblocking and checking signatues.

there's some interesting and tricky question about which of these need to be atomic and how to think about that, or if they're all in queue in sql, and when stuff gets tried again if it fails.

another wrinkle is that the pipeline isn't really linear because dependency checking and missing dependencies happens at multiple stages (a missing key-id, and then once decrypted a missing user, e.g.) and those can be modeled as a simple missing ref. this nudges towards the observer pattern. `all_dependencies_valid_and_included` would be the state we'd want to see.

and then there's an eventbus or something. 

also when we find all valid dependencies we should include the envelopes, so that e.g. secrets are included from a key event, even though the even itself just contains the encapsulated secret. 







