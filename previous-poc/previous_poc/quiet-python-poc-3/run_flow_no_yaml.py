"""Run a protocol flow without using the HTTP-style API (avoids YAML dependency).

This script executes commands directly via core.command.run_command so we can
exercise handlers and projectors without loading api.yaml. Useful for debugging
event projection (create -> invite -> join -> deliver -> process incoming).
"""
import time
from pathlib import Path
from core.db import create_db
from core.command import run_command
import os

# Point handler path to the protocol handlers so run_command can find modules
os.environ['HANDLER_PATH'] = str(Path(__file__).parent / 'protocols' / 'message_via_tor' / 'handlers')
os.environ['CRYPTO_MODE'] = 'dummy'


def run_flow(db_path='demo_no_yaml.db'):
    # Create a fresh DB for the protocol
    db = create_db(db_path=db_path, protocol_name='message_via_tor')

    # 1) Create Alice
    db, res = run_command('identity', 'create', {'name': 'Alice'}, db)
    alice_pub = res.get('api_response', {}).get('identityId')
    print('Alice pubkey:', alice_pub)

    # 2) Alice creates invite
    db, res = run_command('identity', 'invite', {'identityId': alice_pub}, db)
    invite_link = res.get('api_response', {}).get('inviteLink')
    print('Invite link:', invite_link)

    # 3) Bob joins using invite
    db, res = run_command('identity', 'join', {'name': 'Bob', 'inviteLink': invite_link}, db)
    bob_pub = res.get('api_response', {}).get('identity', {}).get('pubkey')
    print('Bob pubkey (from join response):', bob_pub)

    # 4) Run tor simulator deliver to move outgoing -> incoming
    now_ms = int(time.time() * 1000)
    db, res = run_command('tor_simulator', 'deliver', {'time_now_ms': now_ms}, db)
    print('Delivered:', res.get('return'))

    # 5) Process incoming
    db, res = run_command('incoming', 'process_incoming', {'time_now_ms': now_ms}, db)
    print('Processed incoming')

    # 6) Inspect state
    identities = db.get('state', {}).get('identities', [])
    peers = db.get('state', {}).get('peers', [])
    print('\nIDENTITIES:')
    for ident in identities:
        print(' -', ident.get('pubkey'), ident.get('name'))

    print('\nPEERS:')
    for p in peers:
        print(' -', p.get('pubkey'), 'known by', p.get('received_by'))

    return db


if __name__ == '__main__':
    run_flow()
