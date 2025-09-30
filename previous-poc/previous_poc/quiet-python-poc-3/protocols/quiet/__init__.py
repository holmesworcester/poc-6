"""Quiet protocol scaffold: event registry and minimal inbound pipeline.

This package contains:
- event_types: Wire codes, per-type dependency declarations (for hydration),
  and helpers for encryption flags.
- sagas: Minimal processors for inbound transit → parse → wire → encrypted → decrypted.

These are scaffolds to align with the event-centric plan; they do not
integrate with the existing handlers/test runner yet.
"""

