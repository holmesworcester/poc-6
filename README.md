# Quiet Protocol Proof of Concept #6

This is a proof-of-concept protocol for eventually consistent state syncing.

The goal with this attempt is to avoid the framework/protocol distinction of previous attempts and take advantage of the simplicity of building what I need now and nothing else.

I want to keep lines of code to a minimum.

Rules:

- Everything is stored in a single SQLite file
- All application data, local and shared, for all networks, is expressed in an event stource
- We use the "projection" event sourcing pattern where events are "projected" into whatever standard relational database best fits application needs
- A single client can have multiple "peers" (identities) participating in different networks or the same networks.
- To test multiple clients, we test a single client in multiple networks
- For any tests involving a db, "scenario tests" are preferred, and these must *ONLY* build, mock, and test state via realistic "API" usage ("API" in quotes because tests can call functions in queries.py and commands.py directly with their provided params)

Helpful lessons from last time:

- db is not in-memory, it is a real sqlite db
- it is useful to for each event type to have a 
- Goal: functions in each are simple enough that 

# Design 

