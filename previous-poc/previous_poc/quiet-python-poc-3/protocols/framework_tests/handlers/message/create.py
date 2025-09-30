from datetime import datetime

def execute(input_data, db):
    """
    Create a new message event command (simplified version).
    Returns newlyCreatedEvents and any other return values.
    """
    # Get text field
    text = input_data.get("text")
    if not text:
        raise ValueError("Message text is required")
    
    # Create plaintext envelope
    envelope = {
        "envelope": "plaintext",
        "data": {
            "type": "message",
            "content": text,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        },
        "metadata": {
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
    }
    
    # Add replyTo if provided
    reply_to = input_data.get("replyTo")
    if reply_to:
        envelope["data"]["replyTo"] = reply_to
    
    return {
        "newlyCreatedEvents": [envelope],
        "new_events": [{"type": "message", "text": text, "sender": "*"}],
        "return": "Created",
        "messageId": f"msg-{datetime.utcnow().timestamp()}"
    }