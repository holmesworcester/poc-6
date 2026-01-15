# Aspects of Group Message Encryption Design

Encrypted group messaging involves solving several problems at once, and while some of these problems are general to all conceivable schemes, many depend on the specific functionality and security properties users need in a given case.

It's helpful to name the problems explicitly and understand their solutions, to understand which ones fit which cases.

All messages in a group must be encrypted with keys known to recipients and unknown to the attacker. 

# Components

## Defining group membership

What's the source of truth for who is in the group?

Options: 

* **Encrypted, server-side ACL** Signal has a table of members stored on the server but private to the server. (Appropriate to centralized case)
* **Authentication Service** MLS has the notion of an Authentication Service (AS) which *can* potentially be a CRDT but you're on your own for that.
* **Causal Graph of Auth Actions** We can model complex additions and removals as a graph (local-first-web/auth, Keyhive, p2panda). The hard part is having tiebreakers for concurrent changes that match user intuitions and needs (e.g. seniority, a convenient way to tiebreak, is not adequate in workplace teams.
* **Monotonic sets of members, admins, removals** If all removals are valid and all promotions to admin are valid until removal (i.e. two admins can both remove each other, and you can't demote admins) group membership can be a set of added members and a set of removed members, both of which grow monotonically. (I think this is adequate for many groups we are targeting based on our research). In practice this is a causal graph too, but it's a bit simpler this way.

## Message Delivery

Different delivery mechanisms allow for different encryption schemes:

* Client-side fanout (Signal) - enables fully pairwise encryption for group messaging, and simplifies removal (removal = just stop sending to a removed device) 
* Server-side fanout (WhatsApp) 
* P2P set reconciliation (p2p-mode Quiet) - pairwise encryption and a message for each recipient seem impractical in this case, though it might not matter since message attachments would dominate storage cost until groups become very large.
* Hybrid server-side fanout + p2p set reconciliation (server-assisted Quiet) 

One interesting thing here is that if users send enough media (especially video) data is not the cost for pairwise messaging, since media dominates. 

## Key sharing / key agreement

These are the forms of key agreement: 

* **A. Sealed boxes** (Session, TweetNaCl) - Encrypt each message with its own symmetric secret, then seal the secret to each recipient pubkey.
* **B. Sender keys** (WhatsApp) - All devices for all users in the group maintain pairwise keys with each other ("sender keys")
* **C. Group keys** (Local-first-web/auth) - For a given set of members, seal a group key G to each recipient pubkey, then use G until group membership shrinks (rotate on removal). Like sender keys, but if you re-use previously-used keys, provided some mechanism for trusting that the key's view of removal matches your own e.g. by pinning keys to a graph of removals.
* **D. Subset cover** (MLS) - On remove, encrypt new group secrets to a subset of the tree (subtrees) to exclude the removed member. This is like group keys (and is even compatible with sender keys!) but where member devices don't just regularly update their public keys, they also regularly update shared public keys for deterministic subgroups (e.g. common bits in the device hash) and wrap the private keys to their subgroup neighbors.

Space complexity (n users) for decentralized implementations:
 
* Sealed boxes - O(n) asymmetric encryptions per message
* Sender keys - O(n) symmetric encryptions per message, O(n) asymmetric encryptions per user per session
* Group keys - O(1) symmetric encryptions per message, O(n-1) asymmetric per removal
* Subset cover - O(1) symmetric per message, O(logn) per removal (typical case) or O(n) or (maybe O(nlogn)?) worst case.

### How coupled is the key agreement structure to the membership data structure?

In Local-first-web/auth, the key agreement structure is basically the same as the membership data structure, or at least they are tightly coupled: users and groups *are* their lockboxes.

We can imagine a different system where there is a membership CRDT, such as a monotonic set of members and removals, that the key agreement stucture only has the *bare minimum* coupling to. 

For example, if your product can tolerate some small time window where messages could be encrypted to a known removed users, and you trust your admins to rotate keys, you can just accept the latest key from an admin as a reasonable key to use, and admins can wrap keys to the latest members they know about. In this case the two structures use each other but don't have to be linked in any way.

To be stricter, devices can refer to all known removals in each new key, and make a new key whenever they can't find one that excludes all removed users they know about. (Pinning keys to the removal frontier.)

#### Metadata privacy

One thing you can achieve by merging these data structures is making membership of subgroups private to non-members, or making subgroup membership metadata private to the server. That is, you can encrypt the member lists to members. However, this can get very brittle: if the membership state itself gets partitioned there's no way for it to heal: the two sides have know way of knowing about each other! It is also difficult to reason about. 

#### Auth data growth 

Another disadvantage of making both structures the same structure is that while the basic membership data must be stored forever, the cryptographic data does not have to be. 

For example, if your record of membership is a chain of lockboxes, you have to keep all those lockboxes (with all those O(n) key encapsulations) forever! And it grows O(n^2).

If your record of membership is a set of valid invites events, user events, and peer events, that grows O(n) with a small constant.

### Subjective-based or Consensus-based?

It's also useful to think about whether key agreement should be subjective or consensus based.

You probably want some consensus on group membership, but since you might always be partioned or behind, there's always some chance you're sending messages to a user who was already removed on another partition, so the most important guarantee must be the subjective one: "I am sending to the membership list as I see it now."

And in a product where you want joining users to have access to history, you know there might be some new users later who, while currently unknown to you, might see your message. So the important guarantee narrows to the negative one: "I am not sending to any user that I think has been removed."

MLS has a notion of epochs a key and membership set for each epoch, and they require total ordering for this and (I think?) do not acommodate forks.

In Local-first-web/auth, if you're trusting that a given key is the correct one to use for a given group, I would call that consensus-based.

A simple Sealed Boxes approach where every message is encrypted to all known members is very subjective. So is Sender Keys. Each device sends to its own view of the network, assuming other devices share the same view. 

This sounds like a bad idea: what if devices get out of sync? But there's always some possibility of this in the consensus-based approaches too (partitioned removal + join, where a key is rotated on one partition but a join happens on the other) so there's always some need for a "healing" mechanism.

## Removal: How do we secure messages against removed users (PCS)?

* Sealed boxes / sender keys - just stop encrypting to removed users, once you learn they are removed
* Group keys - use a new group key that was not encapsulated to the removed user, once you learn of removal

Related question: **How do we secure future messages against compromised keys of non-removed users? (self-healing / PCS):**

* Rotate public keys regularly, limit public keys / message 

## History Provision

How do we give new users access to old messages? There are two questions:

First, transmission: how is the new user provided old message data?

1. **Set reconciliation** (Quiet, CRDTs) - if the protocol is built around native set reconciliation,new users can sync everything.
2. **History send** - they are sent history on join by the inviter or an online member, or upon request.

Second, decryption: how do new users decrypt old messages? (It might be helpful to think about these separately as "messages sent pre-invite" and "messages since invite")

Pre-invite: in all cases, the inviter can re-seal all keys to a public key in the invite link, and publish some record of the invite, with its public key, that all users can seal messages to. 

This leaves the case of messages that are concurrent to the creation of the invite. 

* **Nested group keys** (Keyhive) - make every (eventual) new key capable of decrypting all old keys. If some old key is left out of the graph of nested keys it will be added as soon as it becomes known (to an admin? or a member with the key?) This conflicts with forward secrecy.
* **Keys upon request** if users/devices have a way of knowing what keys they *should* have (i.e. what groups they are part of) we can give them a way to request keys, provided it is possible for other users to verify that they were not concurrently removed. There's more consideration of this later in the document.

## How do we give new members access to *any* recent messages, when only an untrusted server is available, but no other members are online? 

Same answers as above question, but the case of messages that are concurrent to the creation of the invite is impossible to address. We just don't know who to encrypt these messages to if we don't know about the invite. 

**Question:** is there any way out of this? 

# Forward Secrecy (FS) and Post-Compromise Security (PCS)

## How do we secure past messages against a compromised key? (FS)

* Rotate group keys regularly, purge old keys (including public keys they were sealed to, in case the key-encapsulation messages were captured too) 

## How do we guarantee past deleted messages are unrecoverable given later key compromise?

* The group keys and must be deleted

## How do we ensure other messages that use these keys are still available later, e.g. to a partitioned user?

* Provide them upon request with whatever encryption makes sense (assumes available peer with access to the message plaintext)
* Provide them pre-emptively by rekeying them to a newer, still-in use key
* Hybrid: rekey newer messages, require other peer help with older messages


# Questions with satisfactory answers


## How does auth data (or message overhead) grow over time?

Assume steady churn as % of users (users are removed at a fairly steady rate) and new users send out similar numbers of messages as existing users

* Sealed boxes - message data grows O(n^2); the number of messages is O(n) and the size of each message is O(n).
* Local-first-web/auth - the sigchain is O(n^2) because the number of changes is O(n) and size of each change is O(n) 
* Keyhive - I think it grows O(n(logn)), since removals are O(logn) and it must keep all key data in its structure. TODO: lean more about how compaction works in Keyhive and what data can be dropped 
* Quiet prototyping work - sets of members + removed members is O(n), past keys can be deleted or compacted in the purge/rekey process we use for forward secrecy

In the sealed boxes case, the weight of attachments (which grows O(1) per user for a given message) makes the data cost of encryption overhead insignificant until encryption overhead grows to the same order as the average message attachment. (1,000 - 10,000 users?) -- the real bottleneck here might be not data but sealing performance on mobile.

## How does the performance cost of encrypting messages grow with the number of users?

* Sealed boxes, sender keys - O(n), workable up to 100-1000 devices in a group? (Signal does it with 1000)
* Local-first-web/auth, Keyhive, MLS - O(1)

## How does the performance cost of decrypting messages grow with the number of users?

* Sender keys with message queues and client-side fanout (Signal) - O(1) because you just decrypt the version of each message you receive in your queue. 
* Sealed box / sender keys with server-side fanout - O(1) *if* you reveal each recipient of a message (key hint) or O(n) if you require trial decryption of all sealed keys.
* Local-first-web/auth - all keys are hinted, so group members are revealed, and the group for each message is revealed, in hints. O(1)
* Amigo - O(n) because it uses trial decryption, but it could probably make the same tradeoff of getting O(1) in exchange for hinting.

## Does an admin need to be online for a new member to receive new messages?

* Sender keys - no
* Sealed boxes - no
* Group keys - no
* MLS/Beehive - maybe, but this might not be essential? If only admins can add new users, I think the new user must be added to the tree by an admin. They don't add themselves? 

## If two removals happen, each on its own partition, can all existing members still decrypt all group messages?

Here we consider the naive case with no mitigations or history delivery mechanism:

* Sealed boxes, sender keys - yes: removal just excludes the removed member
* Group keys - yes: each partition now has its own group key, but it has been shared to all members


## How do we let users send messages, when only an untrusted server is available, but no other members are online?

New users need to know what key they can use to send. We can seal it to the invite pubkey.

## Can users who join while partitioned decrypt all group messages?

Here we consider the naive case with no mitigations or history delivery mechanism:

* Sealed boxes, sender keys - no, messages sent on the other partition exclude them until heal
* Group keys - yes, provided there is not a removal on the other partition (same key) in which case no.

Note that in all cases there is some case where a heal is required.

## If a removal happens on one partition, and a new member joins on another, will they eventually decrypt all messages?

* Sealed boxes, sender keys - no: the new user will not be able to read messages sent by users that were not aware of them
* Group keys (including Amigo) - not without some other mechanism for sharing historical keys
* Keyhive - yes: when a new key is created that unites the partitions, each key decrypts its predecessor keys, so users can walk back (sacrifices forward secrecy for convergence) 

**Question:** what is the benefit of Amigo's mathematical merging of keys? Is it just that it constrains the growth of the number of update events by merging them? (logn growth instead of nlogn growth?) -- or is there some other benefit in terms of availability? They tout "users who sync all state can decrypt all messages" but this is true in all group keys designs, except when a joiner is partitioned, the case Amigo fails in too.


## Can users be tricked into failing to encrypt to recipients they intend to encrypt to? (blinding to all messages)

Note: partition healing requires some after-the-fact sharing of messages/keys in all cases; if such a mechanism is in place, exclusion from a group key could slow receipt but is easily corrected. 

Also, in all cases, this is possible when partitioned from a recipient and sending to an already-purged public key.

Otherwise:

* Sealed boxes - No
* Sender keys - No
* Group keys - Yes, a malicious admin (or last rotator) can silently exclude a recipient from a group key share. Proving someone else can unseal something without knowing the private key yourself is a hard problem!
* MLS - Yes (there's some consensus mechanism but I don't think anything checks whichever admin is sealing keys)

Note: this is a place where the consensus based approaches require a healing mechanism, but the subjective-based approaches don't!

## Is some hybrid between sealedbox and group keys useful?

What if we used both sealed box and groupkeys as a default. Sealedbox can provide the message key to <100 recipients who are subjectively members of the group and may not have the group key. 

If we use sealedbox, individual user devices can re-seal their own messages to an inappropriately excluded user, without the danger of revealing messages that weren't intended to be sent to that new user. 

### Consider: "Sealed box with reusable keys"

"Sealed box with reusable keys" is an interesting variant, and you get group keys as an emergent behavior provided that: 

1. other senders can see what devices a previously-used group key claim to have been encapsulated to 
2. there is a healing mechanism ("I, a member, request key k") to protect against de facto removals by unauthorized members 
3. senders re-use other members' keys whenever efficient/expedient (whenever there is a non-expired key that matches the set of users they want to send to)

## How to Get O(logn) Removals, Typical Case, in a Subjective, Decoupled Design

Assuming you already have these things, which you probably need:

0. deterministic, random device id's / identities  
1. a simple membership CRDT (e.g. monotonic sets of members, removed)
2. a removal graph, with keys pinned to / depending on the latest removal
3. a "request missing key" mechanism for members (useful for healing after partitioned joins and malicious rekeys)
4. a regular running task where member devices publish / update their own public keys
5. an O(n) "seal key to recipients" function

...I think you can make it O(logn) in the typical case by swapping out the key publishing and key sealing functions above (items 4 and 5) as follows:

1. For each group, define a binary tree over your device id's
2. For each groupo, have each member device regularly update a public/private keypair for each node they are a member of in the binary tree, and shares the private key to non-removed neighbors at that node.
3. Seal to as few graph nodes and individual public keys as possible to reach the devices you wish to include, without using any keys whose associated removal graph omits a known removal.  

If no devices update their node keys and there keep being removals, key update efficiency degrades to O(n) because only individual keys can be trusted: all the others are known to removed devices. Node keys can have a ttl, with the caveat that expired keys will require devices to request keys. This is also the reason why trees must be per-group: a single tree for all groups is only an efficiency gain if all groups contain most members, which won't be the case.

Finally, senders can re-use previously used keys. With a small number of senders, a high rate of active updates, or a high rate of dormant members who can be safely excluded (they rely on key requests later) we can restrict ourselves to reusing only our own keys ("Sender Keys"). If any of these factors pinch us, we can re-use other group members' keys until removal. 

Once devices re-use previously-used keys, you essentially have the causal treekem protocol.

For forward-secrecy protections for deleted messages, the important thing is to ensure all keys and prekeys associated with the deleted message are also purged within a reasonable time window. To ensure continued availability of those deleted messages to new or offline users, they must be rekeyed.

If removal is just workspace-wide, use one tree. If removal is per group, use a tree for each group.

The important invariants are:
1. Senders must encrypt to all known members, excluding all removed members, using as few keys as possible (including by re-using keys previously created by them or others) 
2. Recipients must be able to see all key events for their group and request new keys (which refer to removals, so that whoever is responding to a key request can ensure the requester is not removed.)


# Questions Without Satisfactory Answers

In these cases there's some materially significant question for which I don't have the answer yet.

## What is the impact on forward secrecy of Keyhive's partition-healing property?

In Keyhive, all keys decrypt previous keys back to root, sacrificing FS for the self-healing property that once a partition is detected, the next key will decrypt both previous keys and "heal" it.

Intuition: forward secrecy is still possible by eliminating keys used for deleted or expired content and rekeying remaining content that uses those keys. 

**Question:** does encrypting previous keys force us to expire *more* keys or is it enough to "tombstone" a link in the chain with a blank that decrypts the previous key and keeps the chain intact? 

Rough answer: If a child node decrypts a parent node, and the parent node is compromised e.g. via the server, the child node could decrypt it. There needs to be some asymmetric/DH operation here, but how would that work? 

## What happens if keys are purged while partitioned?

If keys are purged due to a message being deleted on one partition but not the other, new uses of that pubkey for sealing messages or group keys would fail. 

If keys are only purged at a deterministic cadence, this is less important.

Note: this is a problem if you want asynchronous history for new members but also forward forward secrecy to make deleted messages unrecoverable from surveiled ciphertext and keys from later-compromised devices. 

## Can users send messages to recipients they are not aware of?

Note: if new users are meant to see history, this is intentional/implied. Also, the result is equivalent to a malicioius intended recipient forwarding a message to an unintended recipient, so this is trivially true in all cases and unimportant.

However there is another case where a user believes someone to be removed, but then uses a key on faith that includes the removed user, perhaps because the key creator was partitioned. 

This can only be addressed by making the key refer to or depend on all known removals (and by making removals a graph, so that you can concisely refer to all known removals.) 

## Can normal users "blind" some recipient to a message?

* Sealed boxes, sender keys - Yes
* Group keys - No, unless normal users have the power to remove / rotate keys (which is required to heal from a case where there are concurrent removals and then offline admins.)

## Assuming a server is helping host messages, what can the server see?

* Sender keys - server sees client side fanout, can estimate the membership numbers of different groups? 
* Sealed boxes - can infer group membership size from the size of the key package 
* Group keys - can infer group membership size from rotations
* MLS - can infer group membership size from rotations

(TODO: This should be broken down into a set of subquestions)

## What's the relationship between O(n) removals and "key compaction"?

**TL;DR: O(n) removal could be fine if we are purging and compacting old keys / old removals.**

All conceivable systems will be O(n) on the number of users if only because they need a list of users. A removal is also O(n). 

If removals must be stored forever (e.g. as part of a sigchain) and they are predictably a result of ongoing operation in proportion to the number of users, that gives us O(n^2), because we have one O(n) operation inside another.

However, if we do not need to store removal key sealing messages forever, we get very different properties. In an extreme example, imagine you have 1 day, or 20, to sync the latest keys or you will lose messages. 

The number of removals in that time window does grow in proportion to n, but its growth is much gentler than the total. The limit probably becomes the constant factor in the O(n) removal, i.e., for what n is a single removal just too big? 1MB? 10MB? At that point we are talking thousands removals. (100B for sealed key + hint? So 10MB = 100,000? According to searches, 5-10k seals/s/score are realistic so this is also around where the CPU bottleneck kicks in, or where the size of user data or group membership data itself starts to be a big data cost.)

This is related to deletion, especially timed deletion, and forward secrecy, which also requires purging old keys and moving content we wish to keep to new keys, providing compaction. If we are sufficiently aggressive about not keeping old keys around, that can include not keeping old removal/key-sealing messages around, since we don't have to keep old removal messages for keys we have already had to purge for FS reasons.

## Can we provide missing keys upon request? When is that acceptable? What do we gain from that?

In some partitioned cases (invite & join on one partition while there's a removal on another) there will need to be some self-healing operation or there will be a gap in history for the joiner. There does not seem to be any way around this. 

Given that we cannot strictly meet the requirement "all members will have all keys they need once they (passively) sync all data", what do we gain by relaxing this requirement and relying on some safety net for messages? How can we do it safely?

E.g. if there is a way for a device to request a key it is missing to a group it belongs to, and there is a deterministic way to create a response (so there's no amplification of responses) that would be useful.

## What is the best way for users to reliably know they are being improperly excluded from a key so that they can request a heal?

By definition you have to leak group membership at the key level or the content level or both, so that users can see when they missed something. I think in general it's very difficult to encrypt group membership to the group anyway while maintaining resilience to partitions and forking, but I might be wrong.

## Is "included member notices exclusion" better than "excluded device requests"?

One way to do this would be: have a membership frontier (or a user/peer frontier for the main graph) and members with later frontiers deterministically creating events to add missing members whenever they know of excluded members (members that are in the member frontienr but not in the the removal frontier.) 

This avoids leaks, but it requires some member to notice the exclusion. It also lets all members (or all online members) collude to blind one member of the group without them knowing it, but this is probably possible anyway.

Another question is if users are self-adding, how do user or peer events get added to a graph appropriately? The invite link material would need to include some prior state to refer to, blindly.

## Is deterministic event creation for re-including members practical?

The nice thing is that it solves the problem of too many responses at once and it makes the network traffic burden the same as one responder.

The downside is that everyone has to do it. Also it means these events can't be signed.


## What is a way to provide keys on request that does not let a removed user obtain keys?

One issue is that you don't want to let removed users recover messages sent by a peer that knew of their removal, by asking some peer that had not synced the removal yet. (Is there a way to prove you joined concurrently to a removal?) Making keys depend on prior keys, back to the last removal, would help with this: nobody would know the message context unless they had seen the removal that preceded it. 

**Options:**
1. Require all keys to transitively depend on all known removals at the time the key is created. Then the request can only be granted by peers who have received its removals. Cons: users must sync all removals to sync messages. Pros: dependency ordering / causality to enforce security here; valid requests are clear.
3. Encrypt messages with message-specific keys, then seal those to the group key, then each user can provide access to specific messages to users who request access, who from *their* point of view have not been removed. (The violation only arises when we provide group keys that decrypt others' messages, since those other users may have witnessed the removal.) -- the drawback here is that you need every sender to unlock your missing messages.
4. If invite links have a fixed expiration and are never removed, we can wrap all keys we know about to invite public keys as soon as we become aware of an invite (preserving the guarantee that new users get access to all keys) -- it's unclear who should do this, but the remover could do it once they become aware of a new invite. Then a removed user can only regain access if they use keys from an invite they themselves received, which is equivalent to re-joining as a new user, which they can already do if we don't allow removing/invalidating invite links and rely on only on short invite links and expiration.

Option 1 seems reasonable.

## What would it look like in practice to make each key depend transitively on all prior removals?

Rules:
1. Removals can refer to some number of prior removals.
1. Before rotating the key, "heal" the removal graph by issueing removals until there are no known removals outside the graph. 
2. Issue the new key with a ref to the head of the removal graph.

It might make sense to sync removals first, and separately, to unblock keys and content encrypted to those keys as quickly as possible, and to reduce as much as possible the window where concurrent removals are possible. This also has the desirable effect of limiting what ciphertext removed users are able to sync from users who do not yet know of their removal. 

## Are admins required to "heal" forks that happen after a partition + multiple removals?

In Keyhive, if we have one key for each partition, we need a new key that connects both. However, whoever creates this key can effectively remove a user from the chat (at least temporarily) by failing to seal the new key to their devices.

Answer: if there are two concurrent removals you can get a new key that is unknown to both sides by combining them deterministically, so that users who have both keys know what the new one is. However, if both removed users collude you have a problem! 

Somebody needs to heal by sending a new message, and if you want to be really strict you need to freeze all messaging until this heal happens. If you trust any user to heal you get the problem where that user could potentially remove another. 

This is where the ability to request keys for group epochs you have not been removed from is valuable! If this is possible and there are other online users, you can get the key if somebody does this!

So I think the best solution here is to *not* do commutative operations to combine keys and instead heal by sending a new key (next sender, e.g.) and if there are concurrent heals, have a tiebreaker or just keep healing until you stabilize. The key is being able to know how a given key corresponds to the set of current members (key has to transitively depend on a group id, an admin (maybe?), and all prior removals that define membership, so that any reference to a key depends on all its removals and implies that those removals have been tallied) 

### More notes on this scenario:

if alice and bob get arrested at a protest, but they each get concurrently removed by different admins, can the attacker use keys from both alice and bob to get the new key?

for example, if amigo deterministically derived the "partition healing" new key from the two keys on both branches, neither alice nor bob could derive this key individually, but the police could by combining both alice and bob's keys. 

I think the only way to solve this is to have a "heal" step after the two concurrent removals where you just make a fresh key. 

and then you have to either:

a) only let admins do the heal and block messages until an admin comes online and does it (not great)
b) always give all group members permission to remove others (not great)
c) have some way for users to prove they are members of the group and have not been removed and request new keys (since then you can safely let any member heal the group in their next message, without de facto giving that member the power to remove users.) 

my intuition is that (c) also implies that anyone in the broader network has to be able to see who is a member of what group and who is removed, because they need to be able to say "hey i deserve to have this key and here is the proof I haven't been removed already"---but i'm not sure! 

## Does group membership have to be broadly visible to members of the community for safe key requests to work?

"my intuition is that (c) also implies that anyone in the broader network has to be able to see who is a member of what group and who is removed, because they need to be able to say "hey i deserve to have this key and here is the proof I haven't been removed already"---but i'm not sure!"

So if we encrypted `member` events to the group key... yes that would be a problem because in a partition you wouldn't know about new members from concurrent invite links? Is this right???

Anyway, encrypting membership just gets mind-bending generally and I think creates a lot more fragility. 

## When do TreeKEM updates happen in relation to joining?

First, what updates are we talking about?

I think the only important things to update are the ecrets for each node in the tree. These are only known by other nodes at those tree positions, who might not be online to respond to requests, and who might not be known to joiner yet.

Options:
1. All users / group members members regularly update their node keys and share with fellow co-path members. (Note that they won't *know* that they included all members, but this is okay; it's best for new members to wait some fixed amount of time before updating.)

An optional refinement for adding users: everyone shares existing known node keys deterministically with all new peers (non-removed) when they receive the new peer event. 

(Though if new users have keys through the invite / add member process, this is only useful for covering a gap in keys)

# Questions related to requirements

## Can we relax the requirement that devices that are offline for long periods of time get all messages?

This impacts how much rekeying we have to do for forward secrecy, or whether we have to rekey at all. 

For example, if it's acceptable for our "forward secrecy grace period" after message deletion to be, say, 5 days, and it's acceptable to require devices to come online at least every 5 days to receive all messages, we can achieve forward secrecy by deleting all keys locally after 5 days and only retaining message plaintext, until those messages are expired or deleted. 

(The rekey requirement results from needing to provide messages that used keys we can no longer retain due to our FS commitment to purge keys used to encrypt purged messages.) 

# TODO

It would be useful to have a dashboard that modeled messaging networks with a range of assumptions about user churn, message volume, user data usage, disappearing messages settings, forward secrecy rekey windows, sync performance, bandwidth, mobile crypto performance, performance requirements, etc. that could show us when we're getting into the "red zone" of some requirement not being met.
# Maximally simple, decentralized TreeKEM-style O(logn) messaging

The following is a rough implementation plan for “sender-subjective key selection with efficient removal”. 

By "sender subjective" we mean that each sender is responsible for tracking who is a member, who is a removed, and picking keys that cover the correct membership set from the optimal combination of TreeKEM keys and leaf node public keys.

In a nutshell, you can think of this as a "sender keys" approach where senders wrap a key to every member, except that:

1. Members are constantly posting TreeKEM updates that tend--over time--to offer one key to reach many users (for sending keys efficiently) and small combinations of keys that reach many users **except** a desired subset of excluded users (for sending efficiently after a removal).
2. Senders choose whatever combination of these "reach many" keys and per-recipient keys is the most efficient for reaching everyone.

**Note:** this is intended as an attempt to determine the difficulty of achieving O(logn) scaling in a from-scratch implementation of something like this [Quiet Protocol Draft](https://hackmd.io/lXoX3VAzTU-eLoB9BTupwg) and not as a final design. 

## Phase 1: baseline correctness and healing with O(n) key broadcast

1. Create a pubkey update job that creates a local-only `treekem_secret` event andcorresponding, derived, shared `treekem_pubkey` for each peer (**leaf only**). Make it manually triggered for now and assume a smart trigger; we will discuss triggers later.
1. Start with O(n)-per-message key broadcast. Create a local-only `secret` event and then a **deterministic, unsigned** `secret_shared` event wrapped to each peer’s latest `treekem_pubkey` before each message is sent, using a fresh `secret` for each message (crude).  
   - Deterministic events are not signed; their `event_id` is `H(canonical_event_bytes)` where `canonical_event_bytes` is the canonical encoding of the event (including the key hint + ciphertext).  
   - On projection, decrypting a `secret_shared` deterministically recreates the local-only `secret` keyed by that same `event_id`, so `secret_id` can be used as a hint and for blocking/unblocking.
1. Add a `removal_epoch` event that depends transitively on all previously-seen removals.
1. Make all keying events (`secret_shared`, `treekem_pubkey`, and TreeKEM update events later) reference the latest `removal_epoch` (hard exclusion boundary).
1. Add `key_request` event (rule: removed users cannot request key; more generally, removed users cannot author keying events/messages). **Note on key requests:** All key agreement designs have cases where under partitions the actor circulating keys will be unaware of some member devices and recipients will be missing a key; key requests and responses serves as our catch-all to cover such cases. This also lets you speculatively omit long-inactive users from key broadcast without permanent damage to their view of the network.


Stop here and test various concurrent removal scenarios and ensure inclusion/exclusion. Make sure it’s working well in the O(n) per-message case.

---

## Phase 2: add TreeKEM UpdatePath (hash-trie)

1. Add a TreeKEM-style update operation (still manually triggered—we will address triggering later). When a peer decides to update, it should create a local-only `treekem_secret` and a derived, shared `treekem_pubkey` for each node on **its own leaf→root path** in the binary-trie (hash-trie over stable `peer_id`), up to depth 20.
1. Each `treekem_pubkey` should depend on the previous one in the path (it refs the previous `treekem_pubkey_id`), so the path is a dependency chain.
1. Emit a **signed** `treekem_update` commit event that:
   - references its **author `peer_id`**,
   - references the current `removal_epoch`,
   - references a `base_treekem_update_id` (the winning tree state it is extending / building on), and
   - depends on the final `treekem_pubkey` event (so it transitively depends on the whole path).
1. For each depth on that path, emit exactly one `treekem_secret_shared` event that encrypts that depth’s path secret to the **copath node pubkey** from the referenced base/winning tree state (one ciphertext to the copath node pubkey, not a ciphertext per member).  
   - If a copath node pubkey for a given depth is not available in the sender’s current view, the update simply does not “serve” that copath subtree at that depth yet; those members will rely on **leaf fallback at message send** (or later key requests) until they participate in updates and become represented in the winning tree.
1. When there are multiple conflicting `treekem_update` commits, choose the winning update by the lowest `treekem_update_id`, and apply the entire winning update path as a unit (rather than picking winners node-by-node).

Make sure all tests are passing (including concurrent removals and concurrent updates).

---

## Phase 3: O(log n) removals, and leaf fallback only on message send

### What senders do immediately after a removal

When a sender observes a new `removal_epoch` (i.e., a removal they accept):

- They **must stop using any tree/message keys that could be known to the removed peer**. Concretely: new sends must be keyed to the latest `removal_epoch`.
- On the **first send after removal**, the sender will:
  1. emit a `treekem_update` under the new `removal_epoch` (O(log n)) -- this update may re-use keys/subtrees from the prior winning tree state only where the removed user(s) were not members of that subtree 
  2. broadcast the content key to the newly-updated tree *and* leaf-wrap it to non-removed recipients who are excluded in the new tree state (bounded by an inactivity limit), and rely on `key_request` healing for inactive recipients.

---

### Sending with the tree + leaf fallback policy (no member_epoch)

1. On send, the sender selects a recipient set based on their local membership view (e.g., “group members minus removed”, plus any active invite-link keys if you support history via invite links).
1. The sender encrypts `secret_shared` to a **tree cover** derived from the current **winning** `treekem_update` state (node `treekem_pubkey`s), and then optionally adds **leaf-wrapped** `secret_shared` entries for uncovered recipients, subject to an inactivity limit.
1. **Leaf fallback policy:** treat a member as “covered by the tree” if their `peer_id` appears as an **author of an update included in the current winning tree** (e.g., in the winning chain since the last accepted removal epoch). Leaf-wrap to authorized members whose `peer_id` is not represented in that winning tree view, up to a configured inactivity limit. Members beyond that limit rely on `key_request` healing when they return.

Now the system is:
- **O(1)** per message for the tree-served active set (reuse + tree cover),
- **O(log n)** for updates and removals (UpdatePath),
- and **leaf fallback happens only at message send** under a simple, deterministic policy (“not represented in the winning tree”).

Test various concurrent removal scenarios and ensure inclusion/exclusion.

**Key Takeaways:**
 
Given a system where senders have a subjective view of network membership, as described in the [draft design](https://hackmd.io/lXoX3VAzTU-eLoB9BTupwg), and that choosing appropriate keys is straightforward since keys are labeled with the users they reach and don't reach, the tree update process is the big addition necessary for O(logn) messaging.

Since this seems fairly straightforward too, the hard part seems to be choosing dynamic behaviors that work well for real-world networks, which requires a lot of modeling (and confidence in the models) or realistic simulation.

For example: 

1. What is the optimal threshold for excluding an inactive user and requiring that they request missing keys when they return online? (In large communities, it becomes more and more likely that some online user will know the keys you need and respond quickly.)
2. What is the optimal batching strategy for responses to key requests? How long should responses stick around for?
3. What is the optimal trigger for posting new tree updates (and the optimal number of separate maintained forks?) so that there is always likely to be a new key ready to transition to that already covers most members?
4. When should new clients post their first tree update? (It's helpful if they sync a complete view of network membership and the existing tree first, but when should they be confident that they have?)

# More reading

* https://meri.garden/a-deep-dive-explainer-on-beekem-protocol
* https://github.com/spacelab-ccny/amigo

