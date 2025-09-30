"""
prekey_secret event type: local-only private/public prekey material for sealed KEM.

Public prekey has id `prekey_id`; the local-only secret has id `prekey_secret_id`.
Implementation detail: event IDs are derived from the encrypted blob. Flows
use the stored prekey_secret event's `event_id` as the `prekey_id` for the
public prekey event. Separately, the wire header for sealed messages carries a
16-byte prekey identifier (deterministic from the recipient's public key) used
for routing and opening sealed messages.
"""
