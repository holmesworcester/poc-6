#!/usr/bin/env python3
"""
CLI interface for testing the message_via_tor demo without the TUI.
This allows for automated testing and debugging of the demo functionality.
"""

import json
import sys
import os
from pathlib import Path

# Add the root directory to path for core imports
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Set crypto mode before imports
os.environ['CRYPTO_MODE'] = 'dummy'

try:
    from core.api import execute_api
except ImportError:
    print("Error: Cannot import core.api - missing dependencies")
    print("Using direct command execution instead")
    
    # Fallback to direct command execution
    os.environ["HANDLER_PATH"] = str(Path("protocols/message_via_tor/handlers"))
    from core.db import create_db
    from core.command import run_command
    
    def execute_api(protocol_name, method, path, data=None):
        """Fallback API execution using direct commands"""
        db = create_db(db_path=os.environ.get('API_DB_PATH', 'api.db'), protocol_name=protocol_name)
        
        try:
            # Parse the path to determine handler and command
            if path == "/identities" and method == "POST":
                db, result = run_command("identity", "create", data, db)
            elif path == "/identities" and method == "GET":
                db, result = run_command("identity", "list", {}, db)
            elif path.startswith("/identities/") and path.endswith("/invite"):
                identity_id = path.split("/")[2]
                db, result = run_command("identity", "invite", {"identityId": identity_id}, db)
            elif path == "/join" and method == "POST":
                db, result = run_command("identity", "join", data, db)
            elif path == "/tick":
                from core.tick import tick
                db = tick(db)
                result = {"api_response": {"jobsRun": 5, "eventsProcessed": 0}}
            else:
                return {"status": 404, "body": {"error": f"Unknown endpoint: {method} {path}"}}
            
            # Run tick after commands to process events
            if method == "POST" and path != "/tick":
                from core.tick import tick
                db = tick(db)
            
            api_response = result.get('api_response', {})
            return {
                "status": 201 if method == "POST" else 200,
                "body": api_response
            }
        finally:
            # Always close the database connection
            db.close()

class DemoCLI:
    def __init__(self):
        # Set up SQL database path
        self.db_path = 'demo_cli.db'
        os.environ['API_DB_PATH'] = self.db_path
        
        # Reset database on startup
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception as e:
                print(f"Warning: Could not remove database: {e}")
        
        self.current_identity = None
        self.identities = []
        print("Demo CLI started. Database reset.")
        print("Type /help for commands or 'exit' to quit.")
    
    def refresh_state(self):
        """Fetch current state from the database via API calls"""
        try:
            # Get identities via the list API
            response = execute_api(
                "message_via_tor",
                "GET",
                "/identities",
                data={}
            )
            
            if response.get("status") == 200:
                self.identities = response["body"].get("identities", [])
                print(f"[State refreshed: {len(self.identities)} identities]")
            else:
                print(f"[Failed to refresh state: {response}]")
                self.identities = []
        except Exception as e:
            print(f"[Error refreshing state: {e}]")
            self.identities = []
    
    def handle_create(self, args):
        """Handle /create command"""
        if not args:
            print("[red]Usage: /create <name>[/red]")
            return
        
        name = args.strip()
        print(f"Creating identity '{name}'...")
        
        try:
            response = execute_api(
                "message_via_tor",
                "POST",
                "/identities",
                data={"name": name}
            )
            
            print(f"\n[API Response]")
            print(f"Status: {response.get('status')}")
            print(f"Body: {json.dumps(response.get('body', {}), indent=2)}")
            
            if response.get('status') == 201:
                # Get the created identity ID
                created_id = response.get('body', {}).get('identityId')
                print(f"\n[Success] Identity created with ID: {created_id}")
                
                # Refresh and select
                self.refresh_state()
                
                # Find and select the created identity
                for i, identity in enumerate(self.identities):
                    if identity.get('identityId') == created_id:
                        self.current_identity = i
                        print(f"[Selected identity {i}: {identity.get('name', 'Unknown')}]")
                        break
                else:
                    print(f"[Warning] Created identity not found in refreshed state!")
                    print(f"[Debug] Looking for ID: {created_id}")
                    print(f"[Debug] Available identities: {[id.get('identityId', 'unknown')[:16] + '...' for id in self.identities]}")
            
        except Exception as e:
            print(f"[Error] {e}")
    
    def handle_invite(self):
        """Handle /invite command"""
        print(f"\n[Debug] Current identity index: {self.current_identity}")
        print(f"[Debug] Total identities: {len(self.identities)}")
        
        if self.current_identity is None or self.current_identity < 0:
            print("[Error] No identity selected. Create one with /create <name>")
            return
        
        if self.current_identity >= len(self.identities):
            print(f"[Error] Invalid selection: index {self.current_identity} but only {len(self.identities)} identities")
            self.refresh_state()
            return
        
        identity = self.identities[self.current_identity]
        identity_id = identity.get('identityId', identity.get('publicKey'))
        
        print(f"[Using identity: {identity.get('name', 'Unknown')} (ID: {identity_id[:16]}...)]")
        
        try:
            response = execute_api(
                "message_via_tor",
                "POST",
                f"/identities/{identity_id}/invite",
                data={}
            )
            
            print(f"\n[API Response]")
            print(f"Status: {response.get('status')}")
            print(f"Body: {json.dumps(response.get('body', {}), indent=2)}")
            
            if response.get("status") in [200, 201]:
                invite_link = response["body"].get("inviteLink")
                if invite_link:
                    print(f"\n[Success] Invite link generated:")
                    print(f"{invite_link}")
                else:
                    print("[Error] No invite link in response")
                    
        except Exception as e:
            print(f"[Error] {e}")
    
    def handle_list(self):
        """Handle /list command"""
        self.refresh_state()
        print(f"\n[Identities ({len(self.identities)}):]")
        for i, identity in enumerate(self.identities):
            selected = " [*]" if i == self.current_identity else ""
            print(f"  {i}: {identity.get('name', 'Unknown')} - {identity.get('identityId', 'unknown')[:16]}...{selected}")
    
    def handle_debug(self):
        """Handle /debug command"""
        print("\n[Debug Information]")
        print(f"Current identity index: {self.current_identity}")
        print(f"Database path: {self.db_path}")
        print(f"\nIdentities ({len(self.identities)}):")
        for i, identity in enumerate(self.identities):
            print(f"  [{i}] {json.dumps(identity, indent=4)}")
        
        # Try to get raw database state
        try:
            response = execute_api(
                "message_via_tor",
                "POST",
                "/tick",
                data={}
            )
            if response.get('status') == 200:
                db_state = response.get('body', {}).get('db', {})
                if db_state:
                    print(f"\n[Raw DB State]")
                    print(json.dumps(db_state.get('state', {}), indent=2))
        except Exception as e:
            print(f"[Could not get raw DB state: {e}]")
    
    def handle_select(self, args):
        """Handle /select command"""
        try:
            index = int(args.strip())
            if 0 <= index < len(self.identities):
                self.current_identity = index
                identity = self.identities[index]
                print(f"[Selected identity {index}: {identity.get('name', 'Unknown')}]")
            else:
                print(f"[Error] Invalid index. Use 0-{len(self.identities)-1}")
        except ValueError:
            print("[Error] Usage: /select <index>")
    
    def handle_tick(self):
        """Handle /tick command"""
        try:
            response = execute_api(
                "message_via_tor",
                "POST",
                "/tick",
                data={}
            )
            print(f"\n[Tick Response]")
            print(f"Status: {response.get('status')}")
            print(f"Body: {json.dumps(response.get('body', {}), indent=2)}")
        except Exception as e:
            print(f"[Error] {e}")
    
    def handle_join(self, args):
        """Handle /join command"""
        parts = args.strip().split(maxsplit=1)
        if len(parts) != 2:
            print("[Error] Usage: /join <name> <invite_link>")
            return
        
        name, invite_link = parts
        print(f"Joining as '{name}' with invite link...")
        
        try:
            response = execute_api(
                "message_via_tor",
                "POST",
                "/join",
                data={
                    "name": name,
                    "inviteLink": invite_link
                }
            )
            
            print(f"\n[API Response]")
            print(f"Status: {response.get('status')}")
            print(f"Body: {json.dumps(response.get('body', {}), indent=2)}")
            
            if response.get('status') == 201:
                print(f"\n[Success] Joined network as {name}")
                # Refresh state to get the new identity
                self.refresh_state()
                
                # Find and select the new identity
                body = response.get('body', {})
                new_identity = body.get('identity', {})
                new_pubkey = new_identity.get('pubkey')
                
                if new_pubkey:
                    for i, identity in enumerate(self.identities):
                        if identity.get('identityId') == new_pubkey:
                            self.current_identity = i
                            print(f"[Selected identity {i}: {identity.get('name', 'Unknown')}]")
                            break
            
        except Exception as e:
            print(f"[Error] {e}")
    
    def run(self):
        """Main CLI loop"""
        while True:
            try:
                # Show prompt with current identity
                if self.current_identity is not None and self.current_identity < len(self.identities):
                    identity = self.identities[self.current_identity]
                    prompt = f"[{identity.get('name', 'Unknown')}]> "
                else:
                    prompt = "[No identity]> "
                
                command = input(prompt).strip()
                
                if command.lower() in ['exit', 'quit']:
                    break
                
                if command.startswith('/'):
                    parts = command.split(maxsplit=1)
                    cmd = parts[0].lower()
                    args = parts[1] if len(parts) > 1 else ""
                    
                    if cmd == '/create':
                        self.handle_create(args)
                    elif cmd == '/invite':
                        self.handle_invite()
                    elif cmd == '/list':
                        self.handle_list()
                    elif cmd == '/debug':
                        self.handle_debug()
                    elif cmd == '/select':
                        self.handle_select(args)
                    elif cmd == '/refresh':
                        self.refresh_state()
                    elif cmd == '/tick':
                        self.handle_tick()
                    elif cmd == '/join':
                        self.handle_join(args)
                    elif cmd == '/help':
                        print("\nAvailable commands:")
                        print("  /create <name>  - Create a new identity")
                        print("  /invite         - Generate invite link for current identity")
                        print("  /join <name> <invite_link> - Join network using invite link")
                        print("  /list           - List all identities")
                        print("  /select <index> - Select an identity by index")
                        print("  /debug          - Show debug information")
                        print("  /refresh        - Refresh state from database")
                        print("  /tick           - Run a tick cycle")
                        print("  /help           - Show this help")
                        print("  exit/quit       - Exit the program")
                    else:
                        print(f"Unknown command: {cmd}")
                elif command:
                    print("Regular messages not implemented in CLI. Use /help for commands.")
                    
            except KeyboardInterrupt:
                print("\nUse 'exit' to quit.")
            except Exception as e:
                print(f"Error: {e}")
        
        # Cleanup
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
                print("Database cleaned up.")
            except:
                pass

if __name__ == "__main__":
    cli = DemoCLI()
    cli.run()