Looking at `network.py` 

- Why don't we put the compelx new_network function here instead of in `user.py`? network.create_with_user_device seems more natural for naming. 
- Is there some reason why it's harder to do this work in `network.py` than in `user.py`? 
- Which is better? Weigh.

In `user.py` there are big issues with invite.

```python
   # Note: there are several things here that don't make sense. 
    # I think the original sin is that we have a different function for create_bootstrap_user_invite. 
    # (If we forced ourselves to have the same function we'd have to live within model constraints.)
    # Specific issues:
    # - group_id, channel_id, and key_id are not needed in the invite event at all, ever as far as I can tell. (I thought we had a "slim invite" branch that cut the invite size down)
    # - prekey_id is needed in the normal invite but we can skip it in the first case because we don't need forward secrecy because it's local only. 
    # - the validation rule could be "these are the fields an invite needs if it is signed by a network" vs "these are the fields an invite needs if it is signed by a user"
    # - peer_shared_id will never exist at this point and should not. It is meaningless to have a placeholder for it.

    invite_id, invite_private_key, invite_pubkey = invite.create_bootstrap_user_invite(
        network_id=network_id,
        network_private_key=network_private_key,
        group_id='',  # Not available yet - content created after peer_shared
        channel_id='',  # Not available yet - content created after peer_shared
        key_id='',  # Not available yet - content created after peer_shared
        peer_id=peer_id,
        peer_shared_id='PENDING',  # Will be set to peer_shared_id after it's created
        t_ms=t_ms + 20, # What is this magic number 20 for?
        db=db
    )
    log.info(f"new_network() created bootstrap user invite: {invite_id[:20]}...")
```

In `invite.py`:

- a predicate like `is_admin()` should be a function in `admin.py` not in `invite.py`
- we should make sure other predicates are bound to their events
- `validate` says "DEPRECATED: Not used internally. Use is_admin() instead." We should clean this up, but validate is a good name to have here. If it mostly calls a predicate `is_admin` on the `invite` creator that's great! It will also check signature, no?
- So we use `invite.create_bootstrap_user_invite()` instead of `invite.create()` for bootstrap but that obscures the differences and similarities. We should try to get this into `invite.create()` 
- Looking up `peer_shared_id` in `invite.create()` is bad. We should make this part of params so it doesn't have to be looked up. 
- If everything was always signed by `peer_shared_id` and always included it by default before wrapping, we wouldn't need to worry about it! 
- The key is to exempt network-signed invites from the requirement to be signed by a valid user and peer_shared. Basically network-signed invites can be presumed valid for security reasons. 
- I can see this messing with error messages. 
- What if we extend our idea of `mode` in invite to be mode=peer, mode=user, or mode=first_user but try to keep the differences as minimal and explicit as possible
- In general, in sections of the code like this we should probably have a rule that validation can only exist in projectors, and projectors have to return errors to store which returns them to commands in lieu of event_id. This way we are guaranteed that validation is happening not just at creation (a lazy mistake) -- in other words, create functions always build events "as ordered" without "asking questions" and project enforces strictness. 
- Re: "Look up the admin event that grants admin to this user" this is a tricky subtlety where it's nice to be able to save the API from having to supply this information and just look it up, but this muddies the function's dependencies! Lookups we're doing for convenience as part of event creation seem like they should be segregated from the main flow.
- IMPORTANT re: "Get key from the all_users group" -- this could happen *after* invite creation and be separate from it. I don't know if it even needs to happen at all. If all_users_group is signed by network on community creation we have consensus and we'll all know what it is and share keys to it to all invites! We don't need to make invite depend on an all_users group existing and being projected! 
- `invite_prekey_id = local_prekey_id` this relies on local_prekey events (and id) being deterministic from the actual prekey, ideally for simplicity and consistency with id=hash(event) because the event is generated deterministically based on the key. This is pretty important unless I'm missing something.
- generally the stuff about signing and signed_by and storing I feel like these should be abstracted out of event creation functions to be more dry, no? We have enough stuff to worry about already! 
- Do we really need `group_key_shared.create_for_invite`? Also don't we have to create one for everybody? It seems like it would be simpler to refer to a group_prekey in the invite link, then create group_prekey_shared that prekey, no? 


This is weird. We should create a new transit_prekey here so that we don't have to query or depend on one and error when we don't have one!

Also we should be calling these `inviter_transit_prekey` instead of just `inviter_prekey` so we can keep them straight

```python
    # Get inviter's prekey for Bob to send sync requests
    # Query prekey from transit_prekeys table
    inviter_prekey_row = unsafedb.query_one(
        "SELECT transit_prekey_id, public_key FROM transit_prekeys WHERE owner_peer_id = ? ORDER BY created_at DESC LIMIT 1",
        (peer_id,)

    if not inviter_prekey_row:
        raise ValueError(f"No prekey found for inviter {peer_id}. Cannot create invite.")
    )
```



Is invite_private_key the same one we sign the user event with? Do we need to have separate keys for signing from and and for encrypting to? Or is this a good use of both keys?

```python
# Create local prekey with keypair
    local_prekey_id, invite_private_key = group_prekey.create(peer_id, t_ms + 1, db)
```

`prekey_blob = store.get(local_prekey_id, unsafedb)` <== why do we need unsafedb here? Don't we have a safe way of getting from the store where you can only get events that are recorded by your peer?

Why does `group_prekey_shared` need `peer_id` in addition to `peer_shared` id? We should clean this out. Also peer_shared_id should be signed_by or something, no?

IMPORTANT: For security, if we create a `group_prekey_shared` with a user context for device linking, we must confirm that the prekey is indeed for the user i.e. signed by an existing known peer for that user. Otherwise one could spoof.   

This sentence in `invite.py` is fishy and I wonder if it is deprecated:

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

On this line:

```python
if user_id is not None:
   raise ValueError("mode='user' invites cannot have user_id set")
```

We *could* have a user_id field here. invites do need to be done by admins. The key thing is that this is the inviter_user_id not the invitee_user_id. For simplicity it is probably best to leave the inviter_user_id in both peer and user modes. But leave it out of first_user mode

We should remove these magic values at least, but yes let's make a table and some address you get from the network simulator. 

```python
  # Address info (hardcoded for now, would come from address table in production)
    inviter_ip = '127.0.0.1'
    inviter_port = 6100
```

Okay and the most important part is the creation of the invite event. Do we need the group_id in here? It would be nice if there was another way users could find out about the canonical first group id. It could be signed by the network event, e.g., as part of bootstrapping, after it is created normally, and then everyone would know it once they receive it and receive the network event. I think it makes sense to take it out of here. 

```python
  invite_event_data = {
        'type': 'invite',
        'mode': mode,
        'invite_pubkey': invite_pubkey_b64,  # For user proof signature <== NOTE: this comment should be "for verifying user proof signature"
        'invite_prekey_id': invite_prekey_id,  # Crypto hint for GKS (deterministic hash)
        'group_id': all_users_group_id,  # All users group (for adding joiner)
        'inviter_user_id': inviter_user_id,  # For admin validation during projection
        'signed_by': peer_shared_id,  # Also serves as inviter_peer_shared_id (redundancy removed)
        'created_at': t_ms
    }
```

This seems really problematic because we don't necessarily even have a peer yet! 

```python
    # Sign the invite event with inviter's peer private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_invite_event = crypto.sign_event(invite_event_data, private_key)
```

All of this admins group key stuff is deprecated and should be cleaned out. We don't have that anymore. first `admin` is signed by the network event and can add other admins.

```python
    # Share admins group key (so all users can see who admins are)
    # Get admins group - find by querying groups with network_id that are NOT signed by network
    # (admins group is peer-signed, not network-signed like all_users)
    admin_key_id = None
    admins_group_row = safedb.query_one(
        """SELECT group_id, key_id FROM groups
           WHERE network_id = ? AND signed_by != ? AND recorded_by = ?
           AND name LIKE '% - Admins'
           LIMIT 1""",
        (network_id, network_id, peer_id)
    )

    if admins_group_row:
        admins_group_id = admins_group_row['group_id']
        admin_key_id = admins_group_row['key_id']

        # Share admin group key
        admin_key_shared_id = group_key_shared.create_for_invite(
            key_id=admin_key_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            invite_id=invite_id,
            t_ms=t_ms + 4,
            db=db
        )
        log.info(f"invite.create() created group_key_shared {admin_key_shared_id[:20]}... for admins group key")
```

Okay there are a lot of potential issues here:

```python
invite_link_data = {
        'invite_blob': invite_blob_b64,  # Signed invite event (contains group/channel/key + invite prekey_id)
        'invite_id': invite_id,  # Event ID for reference
        'invite_prekey_id': invite_prekey_id,  # Crypto hint (where Bob stores the key)
        'invite_private_key': crypto.b64encode(invite_private_key),  # Key material for GKS decryption + proof
        'inviter_peer_shared_id': peer_shared_id,  # Alice's peer_shared_id for Bob to send sync requests
        'inviter_peer_shared_blob': inviter_peer_shared_blob_b64,  # Alice's peer_shared blob for immediate projection
        'network_id': network_id,  # For joiner to know which network they're joining
        'channel_id': channel_id,  # Default channel (moved from event to link for immediate access)
        'key_id': key_id,  # Encryption key (moved from event to link for immediate access)
        'ip': inviter_ip,
        'port': inviter_port,
        # Transit prekey fields moved from invite event (for bootstrap contact only)
        'inviter_transit_prekey_public_key': crypto.b64encode(inviter_prekey_public_key),
        'inviter_transit_prekey_shared_id': inviter_transit_prekey_shared_id,
        'inviter_transit_prekey_id': inviter_prekey_id,
```    

`invite_link_data` needs a lot of simplification. Why do we need the `invite_blob`?? People will create events and these will be unblocked if the network event is there. We do need the `invite_id` to reference in our user or peer event and we might want to reference the `network` id too to make sure we recognize the same root as others.


The `peer` blob I can almost see the logic of if it has to do with connections. But I don't think we need it. The `connection` can be bidirectional in the sense that I can send a connection request and anything I get back with that transit_key is known to be for that connection, i.e. authorized, so a sync_request can be authorized and we can sync without any other checks provided the connection is live. We don't need to check sigs on sync requests. The key is that I need to send sync requests to them on the connections I create, *or* I need to know what their full peer_shared_id blob is so I can check signatures. The latter might be better but it feels ugly. We definitely don't *also* need `inviter_peer_shared_id` if we have blob because the blob is its own id (just hash it)

Do we need #channel_id and #key_id in the invite link? This seems like overkill or not needed. Once we sync we will see the channel(s) we have access to. 

We definitely do NOT need both the `inviter_transit_prekey_id` and the `invter_transit_prekey_shared_id`. I think we are just supposed to use the `inviter_transit_prekey_id` from the transit_prekey_shared event because we have it and that's the most direct hint that works like any other dependency. 

We definitely don't have to include the existing user blob. The temptation to include it must have been that we're making an event that depends on it. But we just need the `user_id` and can wait to sync it. The key is projecting enough data straight from `invite_accepted` to *that* table and having connection check that table. Without the existing `peer_shared` blob we might have trouble syncing both ways.

In `cli.py`:

I'm not sure we're using the functions as they are intended to be used. Are we? Generally one command in the CLI should correspond to one API call. Another issue is that this is not sufficiently similar to the joining process. We should have network.create_with_user_device and network.join_as_user_device so that we start from different places but end up in the same one. 

```python
def cmd_join(session: CLISession, username: str, devicename: str, invite_ref: str):
    """Join a network as a NEW user via invite link or number."""
    # Canonicalize: lowercase for consistent storage and display
    devicename = devicename.lower()
    # Resolve invite reference (number or full link)
    invite_link = invite_ref
    invite_display = None  # For event log
    if invite_ref.isdigit():
        invite_num = int(invite_ref)
        resolved_link = session.get_invite_by_number(invite_num)
        if not resolved_link:
            print(f"✗ invite #{invite_num} not found")
            return
        invite_link = resolved_link
        invite_display = f"#{invite_num}"
        print(f"  using invite #{invite_num}")

    # Create the peer first
    peer_id = peer.create(t_ms=session.current_time_ms, db=session.db)

    session.db.commit()
    session.current_time_ms += 100

    # Join the network as a new user
    result = user.join(
        peer_id=peer_id,
        invite_link=invite_link,
        name=username,
        t_ms=session.current_time_ms,
        db=session.db,
        device_name=devicename
    )

    session.db.commit()
    session.current_time_ms += 100
```

Also, logs are basically faked. Like, we're looking up what events are created instead of showing and converting them. We should probably deprecate that feature if the logs aren't meaningful. If there were logs coming from the mechanism that actually creates events (encrypt and store) that'd be great! 

In `invite_accepted.py`:

My understanding of this section is that it should have `network_id` in it because `invite_accepted.project()` is the function that forces the network event to be valid. I'm not even sure we need the invite_id since that flows from the user event we created. 

```python
    event_data = {
        'type': 'invite_accepted',
        'invite_id': invite_id,
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'signed_by': peer_id,
        'created_at': t_ms
    }
```

Another way of thinking about the event data here is that the event data should just be the invite link we got! And the projection should be the moment in parsing the dag when we actually act on this information and begin to use it. I.e. when replaying the event store in a reordering/convergence test, the parsing of the invite_accepted event is the moment when we validate the network event (which we already have but we've had no reason to validate) and everything blocked by it cascades into validity. And the projector is the thing that actually parses the invite accepted event. (Accepting the invite creates the event, which is then projected like any freshly created event. And it's local-only to every creator, never shared.)

There's then the matter of unblocking all events (group_key_shared) wrapped to the prekey private in the invite link. 

BUT there's a more direct way to do this: if group_prekey is deterministic from the key itself, all we have to do is recreate it from the key, project it, and it will have the same event_id, and it will cascade those events valid too. We should make this change! The one hurdle: we have to stop included created_at and signed_by in all local-only key events so that they remain deterministic. That seems fair! 

```python
    # Unblock events that were waiting for this prekey (e.g., group_key_shared events sealed to this invite)
    import queues
    from events.network import recorded as recorded_module
    unblocked_ids = queues.blocked.notify_event_valid(invite_prekey_id, recorded_by, safedb)
    if unblocked_ids:
        log.info(f"invite_accepted.project() unblocked {len(unblocked_ids)} events waiting for invite prekey")
        recorded_module.project_ids(unblocked_ids, db)
```

This is the key place where we deviate from the spec. In the protocol spec, we force the `network` event to be valid upon receipt of the invite link (which should be done in `invite_accepted.project()` I think for replayability). Here we are forcing the invite event to be valid, which then creates a cyclical dependency because the invite event has to depend on the network event etc. I'm not even sure why this code is working.

```python

    # Mark the invite itself as valid (restores out-of-band trust from invite link)
    # This is necessary for reprojection since the invite link is not available
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_id, recorded_by)
    )
```

These should not be in the invite event but rather in the invite link data!

```python
# Extract address/port from invite event (for bootstrap connections)
    # These fields allow send_connect_to_all() to connect to inviter before sync completes
    address = invite_event.get('address')
    port = invite_event.get('port')
```
 
