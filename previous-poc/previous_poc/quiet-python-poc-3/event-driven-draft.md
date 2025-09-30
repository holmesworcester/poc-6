# Narrative version of event-driven framework

I'm going to try a narrative version of this event driven framework with my intended network design, to make sure it makes sense.

We have Events and Prisms (the word sagas or processors might be overloaded so I'll use a new word)

Incoming event is a raw UDP datagram

incoming:

type:incoming, raw_udp_datagram => type:dependencies_unchecked, subtype: encrypted_event, ciphertext, transit_key_id, received_at, origin_ip, origin_port

type:dependencies_unchecked => hold until exist OR => type:dependencies_exist, subtype: encrypted_event, ciphertext, known_transit_key_id, received_at, origin_ip

type:dependencies_exist, subtype: encrypted_event => type: dependencies_unchecked

# second attempt

it is helpful to insert an id checker between each step. checks to make sure all refs to other events are to a known valid events of that event type and holds until true if not. assuming this, we have:

transit_incoming, raw_udp_info => transit_encrypted, ciphertext, transit_key_id, received_at, origin_ip, origin_port

transit_encrypted, ... => network_known_but_unvalidated, payload_fields, network_id_of_transit_key_id, received_at, origin_ip, origin_port

network_incoming => unvalidated_event

(this probably goes through event validators)

unvalidated_event => valid_event

(this goes through event projectors)

valid_event => deltas

deltas => projected_event

# third attempt

it is helpful to include the full events of all valid refs with a valid event. most sagas will not need anything else beyond this to convert to the next event type. 

questions:
- in the expansive design, which events need more information than just knowing all ref event id's are valid and what they are?
- how many event types operate on events other than themselves (delete?)
- what is a plausible way for deletion to work in this mode?
