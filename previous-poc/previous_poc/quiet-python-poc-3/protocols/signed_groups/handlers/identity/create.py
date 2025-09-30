from core import crypto

def execute(params, db):
    """
    Creates an identity with real or dummy keypair based on crypto mode
    """
    name = params.get('name', 'Anonymous')
    
    # Generate keypair using crypto module
    keypair = crypto.generate_keypair()
    pubkey = keypair['public']
    privkey = keypair['private']
    
    # Create identity event
    identity_event = {
        'type': 'identity',
        'pubkey': pubkey,
        'privkey': privkey,
        'name': name
    }
    
    return {
        'api_response': {
            'identityId': pubkey,
            'publicKey': pubkey,
            'name': name
        },
        'newEvents': [identity_event]
    }