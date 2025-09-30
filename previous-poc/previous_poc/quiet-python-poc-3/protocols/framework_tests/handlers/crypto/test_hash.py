from core.crypto import hash

def execute(params, db):
    """Test hash operations with blake2b"""
    data = params["data"]
    
    # Hash with blake2b only
    return {
        "blake2b": hash(data)  # defaults to blake2b
    }