#!/usr/bin/env python3
"""Interactive CLI demo of messaging between two identities"""
import os
import sys
from pathlib import Path
import time

# Setup paths
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))
os.environ['HANDLER_PATH'] = str(project_root / 'protocols' / 'signed_groups' / 'handlers')

from core.db import create_db
from core.command import run_command
from core.tick import tick

def print_section(title):
    print(f"\n{'='*50}")
    print(f"{title}")
    print(f"{'='*50}")

def print_messages(db, channel_id, current_user_name=None):
    cursor = db.conn.cursor()
    messages = list(cursor.execute("""
        SELECT m.content, u.name, m.created_at_ms 
        FROM messages m
        JOIN users u ON m.author_id = u.id
        WHERE m.channel_id = ?
        ORDER BY m.created_at_ms
    """, (channel_id,)))
    
    if messages:
        for content, author_name, timestamp in messages:
            if author_name == current_user_name:
                print(f"  [{author_name} (You)]: {content}")
            else:
                print(f"  [{author_name}]: {content}")
    else:
        print("  (No messages yet)")

# Create test database
db_path = "demo_messaging.db"
if Path(db_path).exists():
    os.unlink(db_path)
db = create_db(db_path)

print_section("SIGNED GROUPS MESSAGING DEMO")
print("\nThis demo shows how two users can message each other in a signed groups network.\n")

# Step 1: Alice creates identity and network
print_section("Step 1: Alice sets up the network")
print("Alice creates her identity...")
db, result = run_command("identity", "create", {"name": "alice"}, db)
alice_id = result['api_response']['identityId']
db = tick(db)
print(f"✓ Alice's identity: {alice_id[:20]}...")

print("\nAlice creates a network...")
db, result = run_command("network", "create", {
    "name": "alice-network",
    "identityId": alice_id
}, db)
network_id = result['api_response']['networkId']
channel_id = result['api_response']['defaultChannelId']
db = tick(db)
print(f"✓ Network created: {network_id}")
print(f"✓ Default channel: #general")

# Get Alice's user ID
cursor = db.conn.cursor()
alice_user = cursor.execute("SELECT id FROM users WHERE pubkey = ?", (alice_id,)).fetchone()
alice_user_id = alice_user[0]

# Step 2: Alice sends first message
print_section("Step 2: Alice sends the first message")
print("Alice is now in the #general channel and sends a message...")
db, result = run_command("message", "create", {
    "channel_id": channel_id,
    "user_id": alice_user_id,
    "peer_id": alice_user_id,
    "content": "Hello! Welcome to my network. Feel free to join!"
}, db)
db = tick(db)
print("\nMessages in #general:")
print_messages(db, channel_id, "alice")

# Step 3: Create invite
print_section("Step 3: Alice creates an invite link")
print("Alice creates an invite link to share...")
db, result = run_command("invite", "create", {"identityId": alice_id}, db)
invite_link = result['api_response']['inviteLink']
db = tick(db)
print(f"\n✓ Invite link created:")
print(f"  {invite_link}")

# Step 4: Bob joins
print_section("Step 4: Bob joins the network")
print("Bob creates his identity...")
db, result = run_command("identity", "create", {"name": "bob"}, db)
bob_id = result['api_response']['identityId']
db = tick(db)
print(f"✓ Bob's identity: {bob_id[:20]}...")

print("\nBob uses the invite link to join...")
db, result = run_command("user", "join", {
    "inviteLink": invite_link,
    "identityId": bob_id
}, db)
db = tick(db)
print("✓ Bob successfully joined the network!")

# Get Bob's user ID
bob_user = cursor.execute("SELECT id FROM users WHERE pubkey = ?", (bob_id,)).fetchone()
bob_user_id = bob_user[0]

print("\nBob can now see the existing messages:")
print_messages(db, channel_id, "bob")

# Step 5: Bob sends a message
print_section("Step 5: Bob sends a message")
print("Bob replies to Alice...")
db, result = run_command("message", "create", {
    "channel_id": channel_id,
    "user_id": bob_user_id,
    "peer_id": bob_user_id,
    "content": "Hi Alice! Thanks for the invite. Happy to be here!"
}, db)
db = tick(db)
print("\nMessages in #general:")
print_messages(db, channel_id, "bob")

# Step 6: Conversation continues
print_section("Step 6: The conversation continues")
print("Alice sees Bob's message and replies...")
db, result = run_command("message", "create", {
    "channel_id": channel_id,
    "user_id": alice_user_id,
    "peer_id": alice_user_id,
    "content": "Great to have you here Bob! How did you find the invite process?"
}, db)
db = tick(db)

print("\nBob responds...")
db, result = run_command("message", "create", {
    "channel_id": channel_id,
    "user_id": bob_user_id,
    "peer_id": bob_user_id,
    "content": "Super easy! I just clicked the link and I was in."
}, db)
db = tick(db)

print("\nCurrent conversation in #general:")
print_messages(db, channel_id)

# Summary
print_section("Summary")
print("✓ Alice created a network with a default #general channel")
print("✓ Alice sent the first message")
print("✓ Alice created an invite link")
print("✓ Bob joined using the invite link")
print("✓ Bob was automatically added to the General group")
print("✓ Both users can see all messages in the channel")
print("✓ Both users can send messages that the other can see")

print("\nThis demonstrates the complete flow of setting up a network")
print("and having two users communicate with each other!")

# Cleanup
db.conn.close()
os.unlink(db_path)