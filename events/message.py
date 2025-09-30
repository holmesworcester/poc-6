def create(params, db, t_ms)
    creator=params.creator;
    props=params;
    plaintext=props;
    key=group.pick_key(params.group.id, db);
    blob=crypto.wrap(plaintext, key, db);
    seen = store.with_seen(blob, creator, t_ms, db);
    projected_seen = seen.project(seen, db)
    channel=params.channel;
    latest=message.list(channel, db);
    db.commit();
    return {
        id: seen_id,
        blob: blob,
        latest: latest
    };