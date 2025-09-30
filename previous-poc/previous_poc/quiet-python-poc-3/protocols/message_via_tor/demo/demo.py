#!/usr/bin/env python3
"""
Message via Tor protocol demo using Textual TUI - API Version.
This version uses API calls exclusively instead of direct database access.

REFACTORED VERSION:
- Clean separation between business logic and UI
- No mocking needed for CLI mode
- Proper state management
"""

import json
import sys
import os
import copy
import time
import re
import asyncio
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Tuple
from enum import Enum

# Add the root directory to path for core imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

# Import the framework's API client
from core.api_client import APIClient

# Change to project root directory
os.chdir(project_root)

# Set handler path for message_via_tor
os.environ['HANDLER_PATH'] = str(project_root / 'protocols' / 'message_via_tor' / 'handlers')


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class CommandResult:
    """Result of executing a command."""
    success: bool
    message: str = ""
    error: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


@dataclass
class PanelState:
    """State of a single panel."""
    identity_name: Optional[str] = None
    identity_pubkey: Optional[str] = None
    messages: List[str] = None
    last_invite_link: Optional[str] = None
    
    def __post_init__(self):
        if self.messages is None:
            self.messages = []


# ============================================================================
# Core Business Logic (No UI Dependencies)
# ============================================================================

class MessageViaTorCore:
    """Core business logic for message via tor demo. No UI dependencies."""
    
    def __init__(self, db_path='demo.db', reset_db=True):
        self.db_path = db_path
        os.environ['API_DB_PATH'] = self.db_path
        
        # Reset database if requested
        if reset_db and os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass
        
        # Initialize API client
        self.api = APIClient("message_via_tor")
        
        # Panel states (1-4)
        self.panels = {i: PanelState() for i in range(1, 5)}
        
        # Track selected identity pubkey for each panel (not index!)
        self.panel_identity_pubkeys = {i: None for i in range(1, 5)}
        
        # Captured variables for CLI scripting
        self.variables = {}
        
        # Event log
        self.events = []
        self.event_counter = 0
        
        # Command history
        self.command_history = []
        
        # Cache for expensive operations
        self._cache = {}
        self._cache_timestamp = 0
        
        # Initialize state
        self.refresh_state()
    
    # ========================================================================
    # State Management
    # ========================================================================
    
    def refresh_state(self, force=False):
        """Fetch current state via API calls."""
        # Simple cache to avoid excessive API calls
        if not force and time.time() - self._cache_timestamp < 1.0:
            return
        
        try:
            # Get all entities via API
            self._cache['identities'] = self._get_entities('identities')
            
            self._cache_timestamp = time.time()
        except Exception as e:
            # Initialize with empty state on error
            self._cache = {
                'identities': []
            }
    
    def _get_entities(self, entity_type):
        """Get entities from API."""
        resp = self.api.get(f"/{entity_type}")
        if resp.get('status') == 200:
            result = resp.get('body', {}).get(entity_type, [])
            # Debug - removed
            return result
        return []
    
    def get_identities(self):
        """Get cached identities."""
        identities = []
        for item in self._cache.get('identities', []):
            # API returns identityId and publicKey
            identities.append({
                'id': item.get('identityId', 'unknown'),
                'name': item.get('name', item.get('identityId', 'Unknown')[:8]),  # Use first 8 chars of ID if no name
                'pubkey': item.get('publicKey', item.get('identityId', 'unknown'))
            })
        return identities
    
    def get_messages_for_identity(self, identity_pubkey):
        """Get messages for a specific identity."""
        if not identity_pubkey:
            return []
        
        resp = self.api.get(f"/messages/{identity_pubkey}", {"limit": 50})
        if resp.get('status') == 200:
            return resp.get('body', {}).get('messages', [])
        return []
    
    def get_panel_identity(self, panel_num):
        """Get the selected identity for a panel."""
        pubkey = self.panel_identity_pubkeys.get(panel_num)
        if not pubkey:
            return None
        
        identities = self.get_identities()
        for identity in identities:
            if identity.get('pubkey') == pubkey:
                return identity
        return None
    
    def set_panel_identity(self, panel_num, identity_pubkey):
        """Set the selected identity for a panel by pubkey."""
        identities = self.get_identities()
        for identity in identities:
            if identity.get('pubkey') == identity_pubkey:
                self.panel_identity_pubkeys[panel_num] = identity_pubkey
                self.panels[panel_num].identity_name = identity.get('name')
                self.panels[panel_num].identity_pubkey = identity_pubkey
                return True
        return False
    
    # ========================================================================
    # Command Implementations
    # ========================================================================
    
    def create_identity(self, panel_num: int, name: str = None) -> CommandResult:
        """Create a new identity."""
        data = {}
        if name:
            data['name'] = name
        
        response = self.api.post("/identities", data)
        
        if response.get("status") == 201:
            body = response.get('body', {})
            identity_id = body.get('identityId')
            pubkey = body.get('publicKey')
            
            # Refresh state to get new identity
            self.refresh_state(force=True)
            
            # Run a tick to process the identity event
            tick_response = self.api.post("/tick", {})
            if tick_response.get("status") == 200:
                # Refresh again after tick
                self.refresh_state(force=True)
            
            # Auto-select in panel
            if pubkey:
                success = self.set_panel_identity(panel_num, pubkey)
                if not success:
                    # If exact match failed, try to find by identity_id
                    identities = self.get_identities()
                    for identity in identities:
                        if identity.get('id') == identity_id:
                            self.panel_identity_pubkeys[panel_num] = identity.get('pubkey')
                            self.panels[panel_num].identity_name = identity.get('name')
                            self.panels[panel_num].identity_pubkey = identity.get('pubkey')
                            break
            
            return CommandResult(
                True, 
                f"Identity created{' with name ' + name if name else ''}!",
                data={'pubkey': pubkey, 'identity_id': identity_id}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def generate_invite(self, panel_num: int) -> CommandResult:
        """Generate an invite link."""
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        response = self.api.post(f"/identities/{identity['pubkey']}/invite", {})
        
        if response.get("status") in [200, 201]:
            body = response.get('body', {})
            invite_link = body.get('inviteLink', '')
            
            if not invite_link:
                return CommandResult(False, error="No invite link in response")
            
            # Store invite link in panel state
            self.panels[panel_num].last_invite_link = invite_link
            
            return CommandResult(
                True,
                f"Invite link generated: {invite_link}",
                data={'invite_link': invite_link}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def join_with_invite(self, panel_num: int, name: str, invite_link: str) -> CommandResult:
        """Join a network using invite code."""
        # Check if panel already has an identity
        if self.panel_identity_pubkeys.get(panel_num) is not None:
            return CommandResult(False, error="Panel already has an identity. Use an empty panel.")
        
        data = {
            'inviteLink': invite_link
        }
        if name:
            data['name'] = name
        
        response = self.api.post("/join", data)
        
        if response.get("status") == 201:
            body = response.get('body', {})
            identity_id = body.get('identityId')
            pubkey = body.get('publicKey')
            
            # Refresh to see new identity
            self.refresh_state(force=True)
            
            # Find and select the new identity
            identities = self.get_identities()
            found = False
            for i, identity in enumerate(identities):
                # Check both ID and pubkey for match
                if (identity_id and identity.get('id') == identity_id) or \
                   (pubkey and identity.get('pubkey') == pubkey) or \
                   (name and identity.get('name') == name):
                    self.panel_identity_pubkeys[panel_num] = identity.get('pubkey')
                    self.panels[panel_num].identity_name = identity.get('name', name)
                    self.panels[panel_num].identity_pubkey = identity.get('pubkey')
                    found = True
                    break
            
            if not found:
                # If exact match not found, select the newest identity
                if identities:
                    identity = identities[-1]
                    self.panel_identity_pubkeys[panel_num] = identity.get('pubkey')
                    self.panels[panel_num].identity_name = identity.get('name', name)
                    self.panels[panel_num].identity_pubkey = identity.get('pubkey')
            
            return CommandResult(
                True,
                f"Joined network as {name}!",
                data={'identity_id': identity_id, 'pubkey': pubkey}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def send_message(self, panel_num: int, text: str) -> CommandResult:
        """Send a message."""
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        response = self.api.post("/messages", {
            "text": text,
            "senderId": identity['pubkey']
        })
        
        if response.get("status") == 201:
            # Add to local message list immediately
            self.panels[panel_num].messages.append(f"{identity['name']}: {text}")
            
            return CommandResult(
                True,
                f"Message sent",
                data={'text': text}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def run_tick(self, count: int = 1) -> CommandResult:
        """Run tick cycles."""
        for i in range(count):
            response = self.api.post("/tick", {})
            if response.get("status") != 200:
                error = response.get('body', {}).get('error', 'Unknown error')
                return CommandResult(False, error=f"Tick failed: {error}")
        
        # Refresh state after ticks
        self.refresh_state(force=True)
        
        return CommandResult(True, f"Ran {count} tick cycle(s)")
    
    def refresh_panel_messages(self, panel_num: int):
        """Refresh messages for a specific panel."""
        panel = self.panels[panel_num]
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return
        
        messages = self.get_messages_for_identity(identity['pubkey'])
        panel.messages.clear()
        
        # Get identity names for display
        identities = self.get_identities()
        pubkey_to_name = {id['pubkey']: id['name'] for id in identities}
        
        for msg in messages:
            sender = msg.get('sender', 'Unknown')
            sender_name = pubkey_to_name.get(sender, sender[:8] + '...')
            content = msg.get('text', '')
            panel.messages.append(f"{sender_name}: {content}")
    
    def refresh_all_messages(self):
        """Refresh messages in all panels."""
        for panel_num in range(1, 5):
            self.refresh_panel_messages(panel_num)
    
    # ========================================================================
    # Event Tracking
    # ========================================================================
    
    def record_event(self, event_type: str, data: Dict[str, Any]):
        """Record an event for the event log."""
        self.event_counter += 1
        self.events.append({
            'id': self.event_counter,
            'type': event_type,
            'data': data,
            'timestamp': time.time()
        })
        # Keep last 100 events
        if len(self.events) > 100:
            self.events = self.events[-100:]
    
    def get_recent_events(self, limit: int = 20):
        """Get recent events from the protocol's actual event store."""
        try:
            resp = self.api.get("/events", {"limit": limit, "order_desc": True})
            if resp.get('status') == 200:
                return resp.get('body', {}).get('events', [])
        except Exception as e:
            # Fallback to local events if API fails
            pass
        
        # Fallback to local UI events
        return list(reversed(self.events[-limit:]))
    
    # ========================================================================
    # State Summary
    # ========================================================================
    
    def get_state_summary(self) -> Dict[str, Any]:
        """Get a summary of current state."""
        return {
            'identities': len(self.get_identities()),
            'panels': {
                i: {
                    'identity': self.panels[i].identity_name,
                    'message_count': len(self.panels[i].messages),
                    'has_invite': self.panels[i].last_invite_link is not None
                }
                for i in range(1, 5)
            }
        }
    
    # ========================================================================
    # Command Processing (used by both CLI and TUI)
    # ========================================================================
    
    def substitute_variables(self, text: str) -> str:
        """Replace $var or ${var} with captured values."""
        def replacer(match):
            var_name = match.group(1) or match.group(2)
            return str(self.variables.get(var_name, f"${{{var_name}}}"))
        
        text = re.sub(r'\$\{(\w+)\}', replacer, text)
        text = re.sub(r'\$(\w+)', replacer, text)
        return text
    
    def execute_panel_command(self, panel_num: int, command: str) -> CommandResult:
        """Execute a command in a specific panel."""
        # Substitute variables
        original_command = command
        command = self.substitute_variables(command)
        
        # Log the command event
        self.event_counter += 1
        event = {
            'id': self.event_counter,
            'type': 'command',
            'panel': panel_num,
            'command': original_command,
            'timestamp': time.time(),
            'data': {}
        }
        
        # Handle slash commands
        result = None
        if command.startswith("/"):
            parts = command.split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            
            event['type'] = f'command:{cmd[1:]}'  # Remove slash for event type
            
            if cmd == "/create":
                result = self.create_identity(panel_num, args if args else None)
                if result.success and result.data:
                    event['data'] = {'name': args, 'pubkey': result.data.get('pubkey', '')}
            elif cmd == "/invite":
                result = self.generate_invite(panel_num)
                if result.success and result.data:
                    event['data'] = {'invite_link': result.data.get('invite_link', '')}
            elif cmd == "/join":
                # Parse name and invite link
                parts = args.split(maxsplit=1)
                if len(parts) < 2:
                    result = CommandResult(False, error="Usage: /join <name> <invite-link>")
                else:
                    name = parts[0]
                    invite_link = parts[1]
                    result = self.join_with_invite(panel_num, name, invite_link)
                    if result.success:
                        event['data'] = {'name': name, 'invite_link': invite_link}
            elif cmd == "/link":
                # Show last invite link
                panel = self.panels[panel_num]
                if panel.last_invite_link:
                    result = CommandResult(True, f"Last invite link: {panel.last_invite_link}")
                else:
                    result = CommandResult(False, error="No invite link generated yet. Use /invite first.")
            elif cmd == "/refresh":
                self.refresh_state(force=True)
                self.refresh_all_messages()
                result = CommandResult(True, "State refreshed")
            else:
                result = CommandResult(False, error=f"Unknown command: {cmd}")
        else:
            # Regular message
            event['type'] = 'message'
            event['data'] = {'content': command}
            result = self.send_message(panel_num, command)
        
        # Add result to event
        event['success'] = result.success if result else False
        event['error'] = result.error if result and result.error else None
        
        # Add event to list
        self.events.append(event)
        
        # Keep only last 100 events
        if len(self.events) > 100:
            self.events = self.events[-100:]
        
        return result if result else CommandResult(False, error="Command processing failed")


# ============================================================================
# CLI Implementation
# ============================================================================

class MessageViaTorCLI(MessageViaTorCore):
    """CLI version of message via tor demo."""
    
    def parse_command(self, cmd_str: str) -> Tuple[Optional[int], str, Optional[str], Optional[str]]:
        """Parse a command string."""
        # Special commands without panel
        if cmd_str.strip() in ['tick', 'refresh'] or cmd_str.strip().startswith(('tick ', 'wait ')):
            return (None, cmd_str.strip(), None, None)
        
        # Parse panel:command format
        match = re.match(r'^(\d+):(.+)$', cmd_str.strip())
        if not match:
            raise ValueError(f"Invalid command format: {cmd_str}")
        
        panel_num = int(match.group(1))
        rest = match.group(2).strip()
        
        # Check for variable capture
        capture_match = re.match(r'^(.+?)\s*->\s*(\w+(?:\.\w+)?)$', rest)
        if capture_match:
            command = capture_match.group(1).strip()
            capture_spec = capture_match.group(2)
            
            if '.' in capture_spec:
                capture_var, capture_field = capture_spec.split('.', 1)
            else:
                capture_var = capture_spec
                capture_field = None
            
            return (panel_num, command, capture_var, capture_field)
        
        return (panel_num, rest, None, None)
    
    def execute_command(self, cmd_str: str) -> CommandResult:
        """Execute a single command string."""
        try:
            panel_num, command, capture_var, capture_field = self.parse_command(cmd_str)
            
            # Record command
            self.command_history.append({
                'command': cmd_str,
                'timestamp': time.time()
            })
            
            if panel_num is None:
                # Special commands
                if command == 'tick':
                    result = self.run_tick()
                elif command.startswith('tick '):
                    count = int(command.split()[1])
                    result = self.run_tick(count)
                elif command == 'refresh':
                    self.refresh_state(force=True)
                    self.refresh_all_messages()
                    result = CommandResult(True, "Refreshed all panels")
                elif command.startswith('wait '):
                    ms = int(command.split()[1])
                    time.sleep(ms / 1000)
                    result = CommandResult(True, f"Waited {ms}ms")
                else:
                    result = CommandResult(False, error=f"Unknown command: {command}")
            else:
                # Panel command
                if panel_num < 1 or panel_num > 4:
                    return CommandResult(False, error=f"Invalid panel number: {panel_num}")
                
                result = self.execute_panel_command(panel_num, command)
            
            # Handle variable capture
            if result.success and capture_var:
                value = None
                if result.data:
                    if capture_field:
                        value = result.data.get(capture_field)
                    else:
                        # Get first value from data
                        value = next(iter(result.data.values())) if result.data else None
                
                if value is not None:
                    self.variables[capture_var] = value
            
            # Refresh messages after commands that might change them
            if result.success and command.startswith(("/", "tick")):
                self.refresh_all_messages()
            
            return result
            
        except Exception as e:
            return CommandResult(False, error=f"Error: {str(e)}")
    
    def format_output(self, format_type: str = 'text') -> str:
        """Format the final output."""
        if format_type == 'json':
            return json.dumps({
                'panels': {
                    i: {
                        'identity': self.panels[i].identity_name,
                        'messages': self.panels[i].messages,
                        'has_invite': self.panels[i].last_invite_link is not None
                    }
                    for i in range(1, 5)
                },
                'state_summary': self.get_state_summary(),
                'events': [e['type'] for e in self.get_recent_events(10)],
                'command_history': [c['command'] for c in self.command_history[-10:]],
                'variables': self.variables
            }, indent=2)
        
        else:  # text format
            output = []
            output.append("=" * 80)
            output.append("MESSAGE VIA TOR DEMO - FINAL STATE")
            output.append("=" * 80)
            
            # Panel states
            for i in range(1, 5):
                panel = self.panels[i]
                if panel.identity_name or panel.messages:
                    output.append(f"\nPANEL {i}:")
                    if panel.identity_name:
                        output.append(f"  Identity: {panel.identity_name}")
                    if panel.identity_pubkey:
                        output.append(f"  Pubkey: {panel.identity_pubkey[:16]}...")
                    if panel.messages:
                        output.append("  Messages:")
                        for msg in panel.messages[-10:]:
                            output.append(f"    {msg}")
            
            # State summary
            output.append("\nSTATE SUMMARY:")
            summary = self.get_state_summary()
            output.append(f"  Identities: {summary['identities']}")
            
            # Recent events
            output.append("\nEVENT SOURCE (newest first):")
            events = self.get_recent_events(5)
            if events:
                for event in events:
                    # Check if it's a protocol event or UI event
                    if 'event_type' in event:
                        # Protocol event
                        output.append(f"  - {event['event_type']}")
                    elif 'type' in event:
                        # UI event
                        output.append(f"  - {event['type']}")
            else:
                output.append("  - No events")
            
            # Variables
            if self.variables:
                output.append("\nCAPTURED VARIABLES:")
                for name, value in self.variables.items():
                    output.append(f"  ${name} = {value}")
            
            # Database State
            output.append("\n" + "=" * 80)
            output.append("DATABASE STATE (from API)")
            output.append("Note: Peer relationships not shown (no API endpoint)")
            output.append("=" * 80)
            output.append(self.get_database_state())
            
            # Event Log
            output.append("\n" + "=" * 80)
            output.append("EVENT LOG (newest first)")
            output.append("=" * 80)
            output.extend(self.get_event_log_display())
            
            output.append("=" * 80)
            return '\n'.join(output)
    
    def get_database_state(self) -> str:
        """Get database state from API for display."""
        try:
            resp = self.api.get("/snapshot")
            if resp.get('status') == 200:
                snapshot = resp.get('body', {})
                # Return either the structured data or SQL dump
                if 'structured' in snapshot:
                    return json.dumps(snapshot['structured'], indent=2)
                elif 'sql_dump' in snapshot:
                    return snapshot['sql_dump']
                else:
                    return json.dumps(snapshot, indent=2)
            else:
                return f"Failed to get snapshot: {resp.get('body', {}).get('error', 'Unknown error')}"
        except Exception as e:
            return f"Error getting database snapshot: {str(e)}"
    
    def get_event_log_display(self) -> List[str]:
        """Get event log for display."""
        output = []
        events = self.get_recent_events(10)
        
        for event in events:
            # Check if this is a protocol event or local UI event
            if 'event_id' in event and 'event_type' in event:
                # Protocol event from event store
                timestamp = time.strftime("%H:%M:%S", time.localtime(event.get('created_at', 0) / 1000))
                
                output.append(f"[{timestamp}] {event['event_type']} (ID: {event['event_id'][:8]}...)")
                output.append(f"  Pubkey: {event['pubkey'][:32]}...")
                
                # Show payload
                if event.get('payload'):
                    output.append("  Payload:")
                    for key, value in event['payload'].items():
                        if isinstance(value, str) and len(value) > 50:
                            value = value[:47] + "..."
                        output.append(f"    {key}: {value}")
                
                # Show metadata if present  
                if event.get('metadata') and any(event['metadata'].values()):
                    output.append("  Metadata:")
                    for key, value in event['metadata'].items():
                        if value:
                            output.append(f"    {key}: {value}")
            else:
                # Local UI event (fallback)
                timestamp = time.strftime("%H:%M:%S", time.localtime(event.get('timestamp', 0)))
                
                if 'panel' in event:
                    output.append(f"[{timestamp}] Event #{event['id']} (Panel {event['panel']})")
                else:
                    output.append(f"[{timestamp}] Event #{event['id']}")
                
                output.append(f"  Type: {event['type']}")
                
                if 'command' in event:
                    output.append(f"  Command: {event['command']}")
                
                if event.get('success') is False and event.get('error'):
                    output.append(f"  ERROR: {event['error']}")
            
            output.append("")
        
        return output

    def run_script(self, commands: List[str], stop_on_error: bool = False, verbose: bool = False) -> bool:
        """Run a list of commands."""
        success = True
        
        for i, cmd_str in enumerate(commands):
            if verbose:
                print(f"\n{'='*80}")
                print(f"[{i+1}/{len(commands)}] Executing: {cmd_str}")
                print('='*80)
            
            result = self.execute_command(cmd_str)
            
            if verbose or not result.success:
                if result.message:
                    print(f"Output: {result.message}")
                if result.error:
                    print(f"ERROR: {result.error}")
            
            # Always show state windows after each command
            if verbose:
                print("\n--- DATABASE STATE (from API) ---")
                print("Note: Peer relationships not shown (no API endpoint)")
                print(self.get_database_state())
                
                print("\n--- EVENT LOG (newest first) ---")
                for line in self.get_event_log_display():
                    print(line)
            
            if not result.success:
                success = False
                if stop_on_error:
                    print(f"\nStopping due to error in command: {cmd_str}")
                    break
        
        return success


# ============================================================================
# TUI Implementation (only UI additions)
# ============================================================================

# Only import Textual if we're actually using TUI mode
if __name__ == "__main__" and ("--run" not in sys.argv and "--script-file" not in sys.argv):
    from textual.app import App, ComposeResult
    from textual.containers import Container, Horizontal, Vertical, VerticalScroll
    from textual.widgets import Header, Footer, Static, Input, Label, RichLog, TextArea, Button
    from textual.message import Message
    from rich.text import Text
    from textual.timer import Timer
    from textual.screen import ModalScreen
    
    class HelpModal(ModalScreen[bool]):
        """Modal to display help information."""
        
        CSS = """
        HelpModal {
            align: center middle;
        }
        
        #help-container {
            width: 60;
            height: 20;
            background: $surface;
            border: thick $primary;
            padding: 1;
        }
        
        #help-title {
            text-align: center;
            text-style: bold;
            color: $text;
            margin-bottom: 1;
        }
        
        #help-content {
            height: 1fr;
        }
        
        #help-footer {
            text-align: center;
            color: $text-disabled;
            margin-top: 1;
        }
        """
        
        def compose(self) -> ComposeResult:
            with Container(id="help-container"):
                yield Label("Message via Tor - Help", id="help-title")
                yield RichLog(id="help-content", wrap=True, markup=True)
                yield Label("Press ESC or ? or click outside to close", id="help-footer")
        
        def on_mount(self) -> None:
            """Display help content when modal is mounted."""
            content = self.query_one("#help-content", RichLog)
            content.write("[bold cyan]Available Commands:[/bold cyan]\n")
            content.write("[green]/create [name][/green] - Create a new identity")
            content.write("[green]/invite[/green] - Generate invite link for current identity")
            content.write("[green]/join <name> <link>[/green] - Join network using invite link")
            content.write("[green]/link[/green] - Show last generated invite link")
            content.write("[green]/refresh[/green] - Refresh state from server")
            content.write("[green]/help[/green] - Show this help\n")
            content.write("[bold cyan]Keyboard Shortcuts:[/bold cyan]")
            content.write("[green]?[/green] - Show this help")
            content.write("[green]Tab[/green] - Switch between panels")
            content.write("[green]Ctrl+C[/green] - Exit application")
            
        def on_key(self, event) -> None:
            """Handle key presses."""
            if event.key == "escape" or event.key == "question_mark":
                self.dismiss(True)
        
        def on_click(self, event) -> None:
            """Handle click events - dismiss if clicked outside the modal."""
            # Get the container's region
            container = self.query_one("#help-container")
            if not container.region.contains(event.x, event.y):
                self.dismiss(True)
    
    class MessageViaTorDemo(MessageViaTorCore, App):
        """TUI version of message via tor demo."""
        
        # Textual app configuration
        TITLE = ""
        SUB_TITLE = ""
        ENABLE_COMMAND_PALETTE = False
        BINDINGS = [
            ("question_mark", "show_help", "Show help"),
        ]
        
        CSS = """
        Screen {
            layout: grid;
            grid-size: 2 2;
            grid-rows: 1fr 1fr;
            grid-gutter: 1;
        }
        
        LoadingIndicator {
            display: none !important;
        }
        
        #controls {
            column-span: 2;
            height: auto;
            dock: top;
            background: $surface;
            border: solid $primary;
            layout: horizontal;
            padding: 0 1;
        }
        
        #controls Button {
            margin: 0 1;
            min-width: 10;
            width: auto;
            height: auto;
            padding: 0 1;
            color: $text;
            align: center middle;
        }
        
        #tick-btn {
            background: $success;
            color: $text;
        }
        
        #refresh-btn {
            background: $warning;  
            color: $text;
        }
        
        #identities-container {
            column-span: 1;
            row-span: 2;
            layout: grid;
            grid-size: 2 2;
            grid-gutter: 1;
        }
        
        #identity1, #identity2, #identity3, #identity4 {
            border: solid blue;
            overflow-y: auto;
        }
        
        #state-inspector {
            column-span: 1;
            row-span: 1;
            border: solid magenta;
            overflow-y: auto;
        }
        
        #event-log {
            column-span: 1;
            row-span: 1;
            border: solid green;
            overflow-y: auto;
        }
        
        .identity-dropdown {
            background: $boost;
            padding: 0 1;
            margin-bottom: 1;
            border: solid $primary;
        }
        
        .messages {
            height: 1fr;
            overflow-y: auto;
        }
        
        Input {
            dock: bottom;
        }
        """
        
        BINDINGS = [
            ("ctrl+t", "tick", "Tick"),
            ("ctrl+r", "refresh", "Refresh"),
            ("tab", "switch_identity", "Switch Identity"),
            ("q", "quit", "Quit"),
            ("ctrl+c", "quit", "Quit"),
        ]
        
        def __init__(self, db_path='demo.db', reset_db=True):
            # Initialize core logic
            MessageViaTorCore.__init__(self, db_path, reset_db)
            
            # Initialize Textual app
            App.__init__(self)
            
            # UI-specific state
            self.is_playing = False
            self.tick_timer = None
            self.invite_displays = {}  # Track invite TextAreas
        
        def compose(self) -> ComposeResult:
            """Create UI widgets."""
            # Control bar
            with Horizontal(id="controls"):
                yield Button("▶️ Play", id="play-pause-btn", variant="primary")
                yield Button("⏯️ Tick", id="tick-btn", variant="success")
                yield Button("🔄 Refresh", id="refresh-btn", variant="warning")
            
            # Identity panels
            with Container(id="identities-container"):
                for i in range(1, 5):
                    with Vertical(id=f"identity{i}"):
                        yield Static(f"Identity {i}: None", classes="identity-dropdown", id=f"identity{i}-dropdown")
                        yield RichLog(classes="messages", id=f"messages{i}", wrap=True, markup=True)
                        yield Input(placeholder="Type message or /help for commands...", id=f"input{i}")
            
            # State inspector
            with VerticalScroll(id="state-inspector"):
                yield Label("State Inspector", classes="identity-label")
                yield RichLog(id="inspector-log", wrap=True, markup=True)
            
            # Event log
            with VerticalScroll(id="event-log"):
                yield Label("Event Source (newest first)", classes="identity-label")
                yield RichLog(id="event-log-display", wrap=True, markup=True)
            
            yield Footer()
        
        def on_mount(self) -> None:
            """Initialize UI on mount."""
            self.update_displays()
        
        def action_show_help(self) -> None:
            """Show help modal."""
            self.push_screen(HelpModal())
        
        def on_button_pressed(self, event: Button.Pressed) -> None:
            """Handle button presses."""
            button_id = event.button.id
            
            if button_id == "play-pause-btn":
                self.toggle_play_pause()
            elif button_id == "tick-btn":
                self.action_tick()
            elif button_id == "refresh-btn":
                self.action_refresh()
        
        def toggle_play_pause(self) -> None:
            """Toggle auto-tick."""
            self.is_playing = not self.is_playing
            play_btn = self.query_one("#play-pause-btn", Button)
            
            if self.is_playing:
                play_btn.label = "⏸️ Pause"
                self.tick_timer = self.set_interval(1.0, self.action_tick)
            else:
                play_btn.label = "▶️ Play"
                if self.tick_timer:
                    self.tick_timer.stop()
                    self.tick_timer = None
        
        def action_tick(self) -> None:
            """Run a tick and update display."""
            result = self.run_tick()
            if result.success:
                self.refresh_all_messages()
                self.update_displays()
        
        def action_refresh(self) -> None:
            """Refresh state and update display."""
            self.refresh_state(force=True)
            self.refresh_all_messages()
            self.update_displays()
        
        def action_switch_identity(self) -> None:
            """Switch identity in focused panel."""
            focused = self.focused
            if not focused or not focused.id.startswith("input"):
                return
            
            panel_num = int(focused.id[-1])
            identities = self.get_identities()
            if not identities:
                return
            
            # Get current identity
            current_pubkey = self.panel_identity_pubkeys.get(panel_num)
            current_idx = -1
            for i, identity in enumerate(identities):
                if identity.get('pubkey') == current_pubkey:
                    current_idx = i
                    break
            
            # Cycle to next identity
            next_idx = (current_idx + 1) % len(identities)
            identity = identities[next_idx]
            
            # Update panel state
            self.panel_identity_pubkeys[panel_num] = identity['pubkey']
            self.panels[panel_num].identity_name = identity['name']
            self.panels[panel_num].identity_pubkey = identity['pubkey']
            
            self.update_displays()
        
        async def on_input_submitted(self, event: Input.Submitted) -> None:
            """Handle input submission."""
            input_id = event.input.id
            if not input_id.startswith("input"):
                return
            
            panel_num = int(input_id[-1])
            text = event.value.strip()
            event.input.value = ""
            
            if not text:
                return
            
            # Process command
            if text.startswith("/"):
                await self.handle_command(panel_num, text)
            else:
                # Send message
                result = self.send_message(panel_num, text)
                messages_log = self.query_one(f"#messages{panel_num}", RichLog)
                
                if result.success:
                    # Message already added to panel.messages by core
                    self.refresh_panel_messages(panel_num)
                    self.update_panel_messages(panel_num)
                else:
                    messages_log.write(f"[red]{result.error}[/red]")
        
        async def handle_command(self, panel_num: int, command: str) -> None:
            """Handle slash commands."""
            messages_log = self.query_one(f"#messages{panel_num}", RichLog)
            
            # Special handling for help - show modal instead
            if command.lower().strip() == "/help":
                self.action_show_help()
                return
            
            # Execute command via core
            result = self.execute_panel_command(panel_num, command)
            
            if result.success:
                messages_log.write(f"[green]{result.message}[/green]")
                
                # Special handling for invite display
                parts = command.split(maxsplit=1)
                cmd = parts[0].lower()
                if cmd == "/invite" and result.data and 'invite_link' in result.data:
                    await self.display_invite(panel_num, result.data['invite_link'])
                
                # Update displays
                self.update_displays()
            else:
                messages_log.write(f"[red]{result.error}[/red]")
        
        async def display_invite(self, panel_num: int, invite_link: str) -> None:
            """Display invite link in a text area."""
            # Remove old invite display if exists
            old_key = f"invite-{panel_num}"
            if old_key in self.invite_displays:
                try:
                    self.invite_displays[old_key].remove()
                except:
                    pass
            
            # Create new invite display
            identity_container = self.query_one(f"#identity{panel_num}")
            invite_display = TextArea(invite_link, read_only=True, id=f"invite-link-{panel_num}")
            invite_display.styles.height = 3
            invite_display.styles.margin = (1, 0)
            
            # Mount after dropdown
            await identity_container.mount(invite_display, after=f"#identity{panel_num}-dropdown")
            self.invite_displays[old_key] = invite_display
        
        def update_displays(self) -> None:
            """Update all UI displays."""
            self.update_panel_displays()
            self.update_state_inspector()
            self.update_event_log()
        
        def update_panel_displays(self) -> None:
            """Update identity panel displays."""
            identities = self.get_identities()
            
            for i in range(1, 5):
                dropdown = self.query_one(f"#identity{i}-dropdown", Static)
                panel = self.panels[i]
                
                # Update dropdown text
                if panel.identity_name:
                    dropdown.update(f"Identity {i}: {panel.identity_name}")
                else:
                    dropdown.update(f"Identity {i}: None")
                
                # Update messages
                self.update_panel_messages(i)
        
        def update_panel_messages(self, panel_num: int) -> None:
            """Update messages display for a panel."""
            messages_log = self.query_one(f"#messages{panel_num}", RichLog)
            messages_log.clear()
            
            panel = self.panels[panel_num]
            
            if not panel.identity_name:
                messages_log.write("[dim]Use /create [name] to create an identity[/dim]")
            else:
                # Show messages
                identity = self.get_panel_identity(panel_num)
                
                for msg in panel.messages[-50:]:  # Last 50 messages
                    # Check if it's our message
                    if identity and msg.startswith(f"{identity['name']}:"):
                        messages_log.write(f"[bold cyan]{msg}[/bold cyan]")
                    else:
                        messages_log.write(f"[green]{msg}[/green]")
                
                if not panel.messages:
                    messages_log.write("[dim]No messages yet. Send a message or wait for incoming messages.[/dim]")
        
        def update_state_inspector(self) -> None:
            """Update state inspector display - shows actual DB state from API."""
            inspector = self.query_one("#inspector-log", RichLog)
            inspector.clear()
            
            inspector.write(Text("DATABASE SNAPSHOT:", style="bold cyan"))
            
            # Fetch database snapshot via /snapshot endpoint
            try:
                resp = self.api.get("/snapshot")
                if resp.get('status') == 200:
                    snapshot = resp.get('body', {})
                    # Display either structured data or SQL dump
                    if 'structured' in snapshot:
                        inspector.write(json.dumps(snapshot['structured'], indent=2))
                    elif 'sql_dump' in snapshot:
                        inspector.write(snapshot['sql_dump'])
                    else:
                        inspector.write(json.dumps(snapshot, indent=2))
                else:
                    inspector.write(f"[red]Failed to get snapshot: {resp.get('body', {}).get('error', 'Unknown error')}[/red]")
            except Exception as e:
                inspector.write(f"[red]Error getting database snapshot: {str(e)}[/red]")
        
        def update_event_log(self) -> None:
            """Update event log display."""
            event_log = self.query_one("#event-log-display", RichLog)
            event_log.clear()
            
            # Show recent events from actual event store
            events = self.get_recent_events(20)
            if not events:
                event_log.write("[dim]No events yet. Execute commands to see them here.[/dim]")
                return
                
            for event in events:
                # Check if this is a protocol event (has event_id, event_type) or local UI event
                if 'event_id' in event and 'event_type' in event:
                    # Protocol event from event store
                    timestamp = time.strftime("%H:%M:%S", time.localtime(event.get('created_at', 0) / 1000))
                    
                    event_log.write(Text(f"\n[{timestamp}] {event['event_type']} ({event['event_id'][:8]}...)", style="bold cyan"))
                    event_log.write(f"Pubkey: {event['pubkey'][:16]}...")
                    
                    # Show payload
                    if event.get('payload'):
                        event_log.write("Payload:")
                        for key, value in event['payload'].items():
                            if isinstance(value, str) and len(value) > 50:
                                value = value[:47] + "..."
                            event_log.write(f"  {key}: {value}")
                    
                    # Show metadata if present
                    if event.get('metadata') and any(event['metadata'].values()):
                        event_log.write("Metadata:")
                        for key, value in event['metadata'].items():
                            if value:  # Only show non-empty values
                                event_log.write(f"  {key}: {value}")
                else:
                    # Local UI event (fallback)
                    timestamp = time.strftime("%H:%M:%S", time.localtime(event.get('timestamp', 0)))
                    
                    # Format event header
                    if 'panel' in event:
                        if event.get('success'):
                            event_log.write(Text(f"\n[{timestamp}] Event #{event['id']} (Panel {event['panel']})", style="bold green"))
                        else:
                            event_log.write(Text(f"\n[{timestamp}] Event #{event['id']} (Panel {event['panel']})", style="bold red"))
                    else:
                        event_log.write(Text(f"\n[{timestamp}] Event #{event['id']}", style="bold yellow"))
                    
                    # Show type and data
                    event_log.write(f"Type: {event.get('type', 'unknown')}")
                    
                    # Show command if present
                    if 'command' in event:
                        event_log.write(f"Command: {event['command']}")
                    
                    # Show data if present
                    if event.get('data'):
                        for key, value in event['data'].items():
                            # Truncate long values
                            if isinstance(value, str) and len(value) > 50:
                                value = value[:47] + "..."
                            event_log.write(f"  {key}: {value}")
                    
                    # Show error if present
                    if event.get('error'):
                        event_log.write(f"[red]Error: {event['error']}[/red]")


# ============================================================================
# Main Entry Point
# ============================================================================

def reset_terminal():
    """Reset terminal to normal state after Textual app exits."""
    import sys
    # Reset mouse tracking modes
    print("\033[?1000l", end="")  # Disable mouse tracking
    print("\033[?1003l", end="")  # Disable any-event mouse tracking  
    print("\033[?1006l", end="")  # Disable SGR mouse mode
    print("\033[?1015l", end="")  # Disable urxvt mouse mode
    
    # Reset other terminal modes
    print("\033[?25h", end="")    # Show cursor
    print("\033[0m", end="")      # Reset colors/attributes
    print("\033[?47l", end="")    # Return to primary screen buffer
    print("\033[?1049l", end="")  # Disable alternate screen buffer
    
    # Flush to ensure all escape codes are sent
    sys.stdout.flush()


def run_cli_mode(args):
    """Run in CLI scripting mode."""
    cli = MessageViaTorCLI(db_path=args.db_path, reset_db=not args.no_reset)
    
    # Collect commands
    commands = []
    if args.run:
        commands.extend(args.run)
    elif args.script_file:
        with open(args.script_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    commands.append(line)
    
    # Execute commands
    success = cli.run_script(
        commands,
        stop_on_error=args.stop_on_error,
        verbose=args.verbose
    )
    
    # Output final state
    print(cli.format_output(args.format))
    
    # Reset terminal after output
    reset_terminal()
    
    return 0 if success else 1


def main():
    """Main entry point."""
    import argparse
    import atexit
    
    # Register terminal reset to run on exit
    atexit.register(reset_terminal)
    
    parser = argparse.ArgumentParser(
        description='Message via Tor Demo (API Version)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
TUI Mode (default):
  %(prog)s                           # Run interactive TUI
  %(prog)s --no-reset                # Keep existing database
  
CLI Script Mode:
  %(prog)s --run "1:/create alice" "2:/create bob" "1:/invite -> link" "2:/join charlie $link"
  %(prog)s --script-file demo.script
  %(prog)s --run "1:/create alice" --format json --verbose
"""
    )
    
    # Common options
    parser.add_argument('--no-reset', action='store_true', 
                        help='Do not reset database on startup')
    parser.add_argument('--db-path', default='demo.db',
                        help='Path to database file')
    
    # CLI mode options
    parser.add_argument('--run', nargs='+', metavar='CMD',
                        help='Run commands in CLI mode')
    parser.add_argument('--script-file', metavar='FILE',
                        help='Read commands from file')
    parser.add_argument('--format', choices=['text', 'json'],
                        default='text', help='Output format for CLI mode')
    parser.add_argument('--stop-on-error', action='store_true',
                        help='Stop execution on first error')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show detailed execution log')
    
    args = parser.parse_args()
    
    # Check if running in CLI mode
    if args.run or args.script_file:
        # CLI mode - don't register atexit in CLI mode to avoid terminal codes in output
        atexit.unregister(reset_terminal)
        sys.exit(run_cli_mode(args))
    else:
        # TUI mode
        try:
            app = MessageViaTorDemo(db_path=args.db_path, reset_db=not args.no_reset)
            app.run()
        except Exception as e:
            # Ensure terminal is reset even on error
            reset_terminal()
            raise


if __name__ == "__main__":
    main()