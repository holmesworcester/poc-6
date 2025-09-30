"""
Protocol-level reflectors mapping.

Map event_type -> callable(envelope, db, time_now_ms) -> (success, envelopes)
"""

from protocols.quiet.events.sync_request.reflector import sync_request_reflector
from protocols.quiet.events.key.reflector import key_reflector

REFLECTORS = {
    'sync_request': sync_request_reflector,
    'key': key_reflector,
}
