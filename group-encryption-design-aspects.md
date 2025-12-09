# Aspects of Group Message Encryption Design

Encrypted group messaging involves solving several problems at once, and while some of these problems are general to all conceivable schemes, many depend on the specific functionality and security properties users need in a given case.

It's helpful to name the problems explicitly and understand their solutions, to understand which ones fit which cases.

All messages in a group must be encrypted with keys known to recipients and unknown to the attacker. 

# Problems

## How do we know who is in the group?

## How do we deliver/model messages

* Client-side fanout (Signal)
* Server-side fanout (WhatsApp)

## Who do we share keys with?

* Session TweetNaCl - Encrypt each message with its own symmetric secret, then seal the secret to each recipient pubkey. ("sealed boxes")
* WhatsApp? - All devices for all users in the groupo maintain pairwise sessions with each other ("sender keys")
* Local-first-web/auth - For a given set of members, seal a group key G to each recipient pubkey, then use G until the group shrinks. ("group keys")
* MLS - On remove, encrypt new group secrets to subtrees that exclude the removed member. ("subset cover")

Space complexity (n users):
 
* Sealed boxes - O(n) asymmetric encryptions per message
* Sender keys - O(n) symmetric encryptions per message, O(n) asymmetric encryptions per user per session
* Group keys - O(1) symmetric encryptions per message, O(n-1) asymmetric per removal
* Subset cover - O(1) symmetric per message, O(logn) per removal

## How do we give new users access to old messages?

* Sender keys - someone must re-encrypt all old messages
* Sealed boxes - inviter must re-seal a key for each old message, users can seal to active invites in all new messages, invitees get private key
* Group keys - same, but inviter must re-seal a key for each past removal/rotation/epoch/group

## How do we secure messages against removed users (PCS)?

* Sealed boxes / sender keys - just stop encrypting to removed users, once you learn they are removed
* Group keys - use a new group key that was not encapsulated to the removed user, once you learn of removal

## How do we secure future messages against compromised keys of non-removed users? (self-healing / PCS)

* Rotate public keys regularly, limit public keys / message 

## How do we secure past messages against a compromised key? (FS)

* Rotate group keys regularly, purge old keys (including public keys they were sealed to, in case the key-encapsulation messages were captured too) 

## How do we guarantee past deleted messages are unrecoverable given later key compromise?

* The group keys and must be deleted

## How do we ensure other messages that use these keys are still available later, e.g. to a partitioned user?

* Provide them upon request with whatever encryption makes sense (assumes available peer with access to the message plaintext)
* Provide them pre-emptively by rekeying them to a newer, still-in use key
* Hybrid: rekey newer messages, require other peer help with older messages

## If two removals happen, each on its own partition, can all existing members still decrypt all group messages?

Here we consider the naive case with no mitigations or history delivery mechanism:

* Sealed boxes, sender keys - yes: removal just excludes the removed member
* Group keys - yes: each partition now has its own group key, but it has been shared to all members

## Can users who join while partitioned decrypt all group messages?

Here we consider the naive case with no mitigations or history delivery mechanism:

* Sealed boxes, sender keys - no, messages sent on the other partition exclude them until heal
* Group keys - yes, provided there is not a removal on the other partition (same key)

## If a removal happens on one partition, and a new member joins on another, will they eventually decrypt all messages?

* Sealed boxes, sender keys - no: the new user will not be able to read messages sent by users that were not aware of them
* Group keys (including Amigo) - not without some other mechanism for sharing historical keys
* Keyhive - yes: when a new key is created that unites the partitions, each key decrypts its predecessor keys, so users can walk back (sacrifices forward secrecy for convergence) 

## What happens if keys are purged while partitioned?

If keys are purged due to a message being deleted on one partition but not the other, new uses of that pubkey for sealing messages or group keys would fail. 

If keys are only purged at a deterministic cadence, this is less important.

Note: this is a problem if you want asynchronous history for new members but also forward forward secrecy to make deleted messages unrecoverable from surveiled ciphertext and keys from later-compromised devices. 

## Can users send messages to recipients they are not aware of?

Note: if new users are meant to see history, this is intentional/implied. Also, the result is equivalent to a malicioius intended recipient forwarding a message to an unintended recipient, so this is trivially true in all cases and unimportant.

## Can normal users "blind" some recipient to a message?

* Sealed boxes, sender keys - Yes
* Group keys - No, unless normal users have the power to remove / rotate keys 

## Can users be tricked into failing to encrypt to recipients they intend to encrypt to? (blinding to all messages)

Note: partition healing requires some after-the-fact sharing of messages/keys in all cases; if such a mechanism is in place, exclusion from a group key could slow receipt but is easily corrected. 

Also, in all cases, this is possible when partitioned from a recipient and sending to an already-purged public key.

Otherwise:

* Sealed boxes - No
* Sender keys - No
* Group keys - Yes, a malicious admin (or last rotator) can silently exclude a recipient from a group key share. Proving someone else can unseal something without knowing the private key yourself is a hard problem!
* MLS - Yes (there's some consensus mechanism but I don't think anything checks whichever admin is sealing keys)