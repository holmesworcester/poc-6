Project Notes

- Negentropy deletion gap: when a message is deleted, we remove it from shareable_events and sometimes from store, but we do not remove its event_id from negentropy_events or update negentropy_buckets. This leaves deleted IDs in the sync set and can cause unnecessary re-sends or mismatch churn. Fix by removing from negentropy tables (and buckets) whenever we delete or skip-if-deleted a shareable event.
