#!/usr/bin/env python3
"""
Signed Groups protocol demo using Textual TUI - API Version.
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

# Set handler path for signed_groups
os.environ['HANDLER_PATH'] = str(project_root / 'protocols' / 'signed_groups' / 'handlers')


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
    network_id: Optional[str] = None
    channel_id: Optional[str] = None
    messages: List[str] = None
    
    def __post_init__(self):
        if self.messages is None:
            self.messages = []


# ============================================================================
# Core Business Logic (No UI Dependencies)
# ============================================================================

class SignedGroupsCore:
    """Core business logic for signed groups demo. No UI dependencies."""
    
    def __init__(self, db_path='signed_groups_demo.db', reset_db=True):
        self.db_path = db_path
        os.environ['API_DB_PATH'] = self.db_path
        
        # Reset database if requested
        if reset_db and os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass
        
        # Initialize API client
        self.api = APIClient("signed_groups")
        
        # Panel states (1-4)
        self.panels = {i: PanelState() for i in range(1, 5)}
        
        # Track selected identity index for each panel
        self.panel_identity_indices = {i: -1 for i in range(1, 5)}
        
        # Captured variables for CLI scripting
        self.variables = {}
        
        # Event log
        self.events = []
        self.event_counter = 0
        
        # Command history
        self.command_history = []
        
        # Cache for expensive operations
        self._cache = {'snapshot': {}}
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
            # Get snapshot of entire database state
            snapshot_resp = self.api.get("/snapshot")
            if snapshot_resp.get('status') == 200:
                snapshot_data = snapshot_resp.get('body', {}).get('structured', {})
                
                # Extract entities from snapshot
                self._cache['identities'] = snapshot_data.get('identities', [])
                self._cache['networks'] = snapshot_data.get('networks', [])
                self._cache['users'] = snapshot_data.get('users', [])
                self._cache['groups'] = snapshot_data.get('groups', [])
                self._cache['channels'] = snapshot_data.get('channels', [])
                self._cache['invites'] = snapshot_data.get('invites', [])
                self._cache['adds'] = snapshot_data.get('adds', [])
                self._cache['blocked'] = snapshot_data.get('blocked', [])
                
                # Store full snapshot for state inspector
                self._cache['snapshot'] = snapshot_data
            else:
                # Fall back to individual API calls if snapshot fails
                self._cache['identities'] = self._get_entities('identities')
                self._cache['networks'] = self._get_entities('networks')
                self._cache['users'] = self._get_entities('users')
                self._cache['groups'] = self._get_entities('groups')
                self._cache['channels'] = self._get_entities('channels')
                self._cache['invites'] = self._get_entities('invites')
                self._cache['adds'] = []  # No adds endpoint, would need to be in snapshot
            
            # Get events separately (they have their own endpoint with ordering/limiting)
            events_resp = self.api.get("/events", {"limit": 20})
            self._cache['events'] = events_resp.get('body', {}).get('events', []) if events_resp.get('status') == 200 else []
            
            self._cache_timestamp = time.time()
        except Exception as e:
            # Initialize with empty state on error
            self._cache = {
                'identities': [], 'networks': [], 'users': [],
                'groups': [], 'channels': [], 'invites': [], 'adds': [], 'events': [],
                'snapshot': {}
            }
    
    def _get_entities(self, entity_type):
        """Get entities from API."""
        resp = self.api.get(f"/{entity_type}")
        if resp.get('status') == 200:
            return resp.get('body', {}).get(entity_type, [])
        return []
    
    def get_identities(self):
        """Get cached identities."""
        return self._cache.get('identities', [])
    
    def get_networks(self):
        """Get cached networks."""
        return self._cache.get('networks', [])
    
    def get_users(self, network_id=None):
        """Get users, optionally filtered by network."""
        users = self._cache.get('users', [])
        if network_id:
            return [u for u in users if u.get('network_id') == network_id]
        return users
    
    def get_groups(self, network_id=None):
        """Get groups, optionally filtered by network."""
        groups = self._cache.get('groups', [])
        if network_id:
            # Filter groups by network_id
            return [g for g in groups if g.get('network_id') == network_id]
        return groups
    
    def get_channels(self, network_id=None):
        """Get channels, optionally filtered by network."""
        channels = self._cache.get('channels', [])
        if network_id:
            return [c for c in channels if c.get('network_id') == network_id]
        return channels
    
    def get_accessible_channels(self, user_id):
        """Get channels accessible to a user (in groups they belong to)."""
        if not user_id:
            return []
        
        # Get user to find their network
        users = self._cache.get('users', [])
        user = None
        for u in users:
            if u['id'] == user_id:
                user = u
                break
        
        if not user or not user.get('network_id'):
            return []
        
        network_id = user['network_id']
        
        # Get user's groups (including if they created the group)
        adds = self._cache.get('adds', [])
        groups = self._cache.get('groups', [])
        
        # User is in groups they were added to OR created
        user_groups = [add['group_id'] for add in adds if add['user_id'] == user_id]
        created_groups = [g['id'] for g in groups if g.get('created_by') == user_id]
        user_groups.extend(created_groups)
        user_groups = list(set(user_groups))  # Remove duplicates
        
        # Also include the group from the user's invite if they joined via invite
        if user.get('group_id') and user['group_id'] not in user_groups:
            user_groups.append(user['group_id'])
        
        # Get channels in those groups, filtered by network
        all_channels = self.get_channels(network_id)  # Filter by network
        accessible = []
        for channel in all_channels:
            if channel.get('group_id') in user_groups:
                # Add group info for display
                groups = self.get_groups()  # Get all groups since they don't have network_id
                for group in groups:
                    if group['id'] == channel.get('group_id'):
                        channel['group_name'] = group.get('name', 'Unknown')
                        break
                accessible.append(channel)
        
        return accessible
    
    def get_messages_for_channel(self, channel_id):
        """Get messages for a specific channel."""
        if not channel_id:
            return []
        
        resp = self.api.get("/messages", {"channel_id": channel_id})
        if resp.get('status') == 200:
            return resp.get('body', {}).get('messages', [])
        return []
    
    def get_panel_identity(self, panel_num):
        """Get the selected identity for a panel."""
        identities = self.get_identities()
        idx = self.panel_identity_indices.get(panel_num, -1)
        if 0 <= idx < len(identities):
            return identities[idx]
        return None
    
    def get_user_for_identity(self, identity_pubkey):
        """Find user for an identity pubkey."""
        users = self.get_users()
        return next((u for u in users if u.get('pubkey') == identity_pubkey), None)
    
    def set_panel_identity(self, panel_num, identity_pubkey):
        """Set the selected identity for a panel by pubkey."""
        identities = self.get_identities()
        for i, identity in enumerate(identities):
            if identity.get('pubkey') == identity_pubkey:
                self.panel_identity_indices[panel_num] = i
                self.panels[panel_num].identity_name = identity.get('name')
                self.panels[panel_num].identity_pubkey = identity_pubkey
                return True
        return False
    
    # ========================================================================
    # Command Implementations
    # ========================================================================
    
    def create_identity(self, panel_num: int, name: str) -> CommandResult:
        """Create a new identity."""
        if not name:
            return CommandResult(False, error="Usage: /create <name>")
        
        # Check if panel already has an identity
        if self.panels[panel_num].identity_pubkey:
            return CommandResult(False, error="This panel already has an identity. Use a different panel to create another identity.")
        
        response = self.api.post("/identities", {"name": name})
        
        if response.get("status") == 201:
            body = response.get('body', {})
            pubkey = body.get('identityId')
            
            # Refresh state to get new identity
            self.refresh_state(force=True)
            
            # Auto-select in panel
            if pubkey:
                self.set_panel_identity(panel_num, pubkey)
            
            return CommandResult(
                True, 
                f"Identity '{name}' created!",
                data={'pubkey': pubkey}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def create_network(self, panel_num: int, name: str) -> CommandResult:
        """Create a new network."""
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        response = self.api.post("/networks", {
            "name": name,
            "identityId": identity['pubkey']
        })
        
        if response.get("status") == 201:
            body = response.get('body', {})
            network_id = body.get('networkId')
            channel_id = body.get('defaultChannelId')
            
            # Update panel state
            panel = self.panels[panel_num]
            panel.network_id = network_id
            if channel_id:
                panel.channel_id = channel_id
            
            # Refresh to see new entities
            self.refresh_state(force=True)
            
            return CommandResult(
                True,
                f"Network '{name}' created with default channel!",
                data={'network_id': network_id, 'channel_id': channel_id}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def generate_invite(self, panel_num: int) -> CommandResult:
        """Generate an invite link."""
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        # Check if user exists
        user = self.get_user_for_identity(identity['pubkey'])
        if not user:
            return CommandResult(False, error="Must be registered as a user first. Use /network <name>")
        
        response = self.api.post("/invites", {
            "identityId": identity['pubkey']
        })
        
        if response.get("status") == 201:
            body = response.get('body', {})
            invite_link = body.get('inviteLink', '')
            
            if not invite_link:
                return CommandResult(False, error="No invite link in response")
            
            return CommandResult(
                True,
                f"Invite link generated: {invite_link}",
                data={'invite_link': invite_link}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def join_with_invite(self, panel_num: int, invite_code: str) -> CommandResult:
        """Join a network using invite code."""
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        # Check if already a user
        existing_user = self.get_user_for_identity(identity['pubkey'])
        if existing_user:
            return CommandResult(False, error="Identity already registered in network")
        
        response = self.api.post("/users/join", {
            "inviteLink": invite_code,
            "identityId": identity['pubkey']
        })
        
        if response.get("status") in [200, 201]:
            body = response.get('body', {})
            network_id = body.get('networkId')
            user_id = body.get('userId')
            accessible_channels = body.get('accessibleChannels', [])
            default_channel_id = body.get('defaultChannelId')
            
            if os.environ.get('VERBOSE'):
                print(f"[DEBUG] Join response: channels={len(accessible_channels)}, default={default_channel_id}")
                for ch in accessible_channels:
                    print(f"[DEBUG]   Channel: {ch}")
            
            # Update panel state
            self.panels[panel_num].network_id = network_id
            
            # Select the default channel if provided
            if default_channel_id:
                self.panels[panel_num].channel_id = default_channel_id
                if os.environ.get('VERBOSE'):
                    print(f"[DEBUG] Set panel {panel_num} channel to {default_channel_id}")
                
            # Refresh to see new user and load messages
            self.refresh_state(force=True)
            # Messages will be refreshed on next interaction
            
            return CommandResult(
                True,
                f"Joined network successfully!",
                data={
                    'network_id': network_id,
                    'user_id': user_id,
                    'accessible_channels': accessible_channels,
                    'default_channel_id': default_channel_id
                }
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def create_channel(self, panel_num: int, name: str, group_name: str = None) -> CommandResult:
        """Create a channel."""
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        user = self.get_user_for_identity(identity['pubkey'])
        if not user:
            return CommandResult(False, error="Must be registered as a user first")
        
        network_id = user.get('network_id')
        if not network_id:
            return CommandResult(False, error="User has no network")
        
        # Find group - get all groups since they don't have network_id
        groups = self.get_groups()
        if not groups:
            return CommandResult(False, error="No groups available")
        
        group_id = None
        if group_name:
            for group in groups:
                if group['name'].lower() == group_name.lower():
                    group_id = group['id']
                    break
            if not group_id:
                return CommandResult(False, error=f"Group '{group_name}' not found")
        else:
            group_id = groups[0]['id']
        
        response = self.api.post("/channels", {
            "name": name,
            "network_id": network_id,
            "user_id": user['id'],
            "group_id": group_id
        })
        
        if response.get("status") == 201:
            body = response.get('body', {})
            channel_id = body.get('channelId')
            
            # Auto-select channel
            if channel_id:
                self.panels[panel_num].channel_id = channel_id
            
            # Refresh to see new channel
            self.refresh_state(force=True)
            
            return CommandResult(
                True,
                f"Channel '{name}' created!",
                data={'channel_id': channel_id}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def create_group(self, panel_num: int, name: str) -> CommandResult:
        """Create a group."""
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        user = self.get_user_for_identity(identity['pubkey'])
        if not user:
            return CommandResult(False, error="You need to create or join a network first")
        
        response = self.api.post("/groups", {
            "name": name,
            "user_id": user['id']
        })
        
        if response.get("status") == 201:
            body = response.get('body', {})
            group_id = body.get('groupId')
            
            # Refresh to see new group
            self.refresh_state(force=True)
            
            return CommandResult(
                True,
                f"Group '{name}' created!",
                data={'group_id': group_id}
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
    def send_message(self, panel_num: int, text: str) -> CommandResult:
        """Send a message."""
        panel = self.panels[panel_num]
        
        if not panel.channel_id:
            return CommandResult(False, error="Must be in a channel to send messages")
        
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        user = self.get_user_for_identity(identity['pubkey'])
        if not user:
            return CommandResult(False, error="User not found")
        
        response = self.api.post("/messages", {
            "channel_id": panel.channel_id,
            "user_id": user['id'],
            "peer_id": user['id'],
            "content": text
        })
        
        if response.get("status") == 201:
            # Add to local message list immediately
            panel.messages.append(f"{identity['name']}: {text}")
            
            return CommandResult(
                True,
                f"Message sent",
                data={'content': text}
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
        if panel.channel_id:
            messages = self.get_messages_for_channel(panel.channel_id)
            panel.messages.clear()
            
            # Get user names
            users = self.get_users()
            user_map = {u['id']: u['name'] for u in users}
            
            for msg in messages:
                author = user_map.get(msg.get('user_id'), 'Unknown')
                content = msg.get('content', '')
                panel.messages.append(f"{author}: {content}")
    
    def refresh_all_messages(self):
        """Refresh messages in all panels."""
        for panel_num in range(1, 5):
            self.refresh_panel_messages(panel_num)
    
    def switch_to_channel(self, panel_num: int, channel_name: str) -> CommandResult:
        """Switch to a channel by name."""
        if not channel_name:
            return CommandResult(False, error="Usage: /switch <channel-name>")
        
        # Get accessible channels for this panel
        channels = self.get_panel_accessible_channels(panel_num)
        if not channels:
            return CommandResult(False, error="No accessible channels")
        
        # Find channel by name (case insensitive)
        channel_name_lower = channel_name.lower()
        matched_channel = None
        for channel in channels:
            if channel.get('name', '').lower() == channel_name_lower:
                matched_channel = channel
                break
        
        if not matched_channel:
            # Show available channels
            available = [ch.get('name', 'unnamed') for ch in channels]
            return CommandResult(False, error=f"Channel '{channel_name}' not found. Available: {', '.join(available)}")
        
        # Switch to the channel
        self.panels[panel_num].channel_id = matched_channel['id']
        self.refresh_panel_messages(panel_num)
        
        return CommandResult(True, f"Switched to channel #{matched_channel.get('name', 'unnamed')}")
    
    def get_panel_accessible_channels(self, panel_num: int) -> List[Dict[str, Any]]:
        """Get accessible channels for a panel's current identity."""
        panel = self.panels[panel_num]
        if not panel.identity_pubkey:
            return []
        
        user = self.get_user_for_identity(panel.identity_pubkey)
        if not user:
            return []
        
        return self.get_accessible_channels(user['id'])
    
    def add_user_to_group(self, panel_num: int, args: str) -> CommandResult:
        """Add a user to a group. Usage: /add <user_name> to <group_name>"""
        if ' to ' not in args:
            return CommandResult(False, error="Usage: /add <user_name> to <group_name>")
        
        user_name, group_name = args.split(' to ', 1)
        user_name = user_name.strip()
        group_name = group_name.strip()
        
        # Get the current identity (must be the one doing the adding)
        identity = self.get_panel_identity(panel_num)
        if not identity:
            return CommandResult(False, error="No identity selected")
        
        adder = self.get_user_for_identity(identity['pubkey'])
        if not adder:
            return CommandResult(False, error="You need to create or join a network first before adding users to groups")
        
        # Find the user to add by name
        users = self.get_users()
        target_user = None
        for user in users:
            if user.get('name', '').lower() == user_name.lower():
                target_user = user
                break
        
        if not target_user:
            # Check if they exist as an identity but haven't joined a network
            identities = self.get_identities()
            identity_exists = any(i.get('name', '').lower() == user_name.lower() for i in identities)
            if identity_exists:
                return CommandResult(False, error=f"'{user_name}' has not joined any network yet. They need to join using an invite link first.")
            else:
                return CommandResult(False, error=f"No identity named '{user_name}' found")
        
        # Check if both users are in the same network
        if target_user.get('network_id') != adder.get('network_id'):
            return CommandResult(False, error=f"'{user_name}' is in a different network. Users must be in the same network to share groups.")
        
        # Find the group by name
        groups = self.get_groups()
        target_group = None
        for group in groups:
            if group.get('name', '').lower() == group_name.lower():
                target_group = group
                break
        
        if not target_group:
            return CommandResult(False, error=f"Group '{group_name}' not found")
        
        # Make the API call to add user to group
        response = self.api.post("/group-memberships", {
            "user_id": target_user['id'],
            "group_id": target_group['id'],
            "added_by": adder['id']
        })
        
        if response.get("status") == 201:
            # Refresh to see the changes
            self.refresh_state(force=True)
            
            return CommandResult(
                True,
                f"Added {user_name} to group {group_name}!",
                data={
                    'user_id': target_user['id'],
                    'group_id': target_group['id']
                }
            )
        else:
            error = response.get('body', {}).get('error', 'Unknown error')
            return CommandResult(False, error=f"Failed: {error}")
    
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
        """Get recent events."""
        return list(reversed(self.events[-limit:]))
    
    def get_protocol_events(self, limit: int = 20):
        """Get recent protocol events from the API cache."""
        events = self._cache.get('events', [])
        return events[:limit]  # Already newest first from API
    
    # ========================================================================
    # State Summary
    # ========================================================================
    
    def get_state_summary(self) -> Dict[str, Any]:
        """Get a summary of current state."""
        # If we have snapshot data, include more details
        if 'snapshot' in self._cache:
            summary = {
                'database_tables': {}
            }
            for table, rows in self._cache['snapshot'].items():
                if rows:
                    summary['database_tables'][table] = len(rows)
        else:
            summary = {
                'identities': len(self.get_identities()),
                'networks': len(self.get_networks()),
                'users': len(self.get_users()),
                'groups': len(self.get_groups()),
                'channels': len(self.get_channels())
            }
        
        summary['panels'] = {
            i: {
                'identity': self.panels[i].identity_name,
                'network_id': self.panels[i].network_id,
                'channel_id': self.panels[i].channel_id,
                'message_count': len(self.panels[i].messages)
            }
            for i in range(1, 5)
        }
        
        return summary
    
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
                result = self.create_identity(panel_num, args)
                if result.success and result.data:
                    event['data'] = {'name': args, 'pubkey': result.data.get('pubkey', '')}
            elif cmd == "/network":
                result = self.create_network(panel_num, args)
                if result.success and result.data:
                    event['data'] = {'name': args, 'network_id': result.data.get('network_id', '')}
            elif cmd == "/invite":
                result = self.generate_invite(panel_num)
                if result.success and result.data:
                    event['data'] = {'invite_link': result.data.get('invite_link', '')}
            elif cmd == "/join":
                result = self.join_with_invite(panel_num, args)
                if result.success:
                    event['data'] = {'invite_code': args}
            elif cmd == "/channel":
                # Parse optional group
                if " in " in args:
                    channel_name, group_name = args.split(" in ", 1)
                    result = self.create_channel(panel_num, channel_name.strip(), group_name.strip())
                    event['data'] = {'channel': channel_name.strip(), 'group': group_name.strip()}
                else:
                    result = self.create_channel(panel_num, args)
                    event['data'] = {'channel': args}
                if result and result.success and result.data:
                    event['data']['channel_id'] = result.data.get('channel_id', '')
            elif cmd == "/group":
                result = self.create_group(panel_num, args)
                if result and result.success and result.data:
                    event['data'] = {'name': args, 'group_id': result.data.get('group_id', '')}
            elif cmd == "/add":
                # Add user to group
                result = self.add_user_to_group(panel_num, args)
                if result.success:
                    event['data'] = {'args': args}
            elif cmd == "/refresh":
                self.refresh_state(force=True)
                self.refresh_all_messages()
                result = CommandResult(True, "State refreshed")
            elif cmd == "/tick":
                # Run a tick cycle to process background jobs
                response = self.api.post("/tick", {})
                if response.get('status') == 200:
                    body = response.get('body', {})
                    jobs_run = body.get('jobsRun', 0)
                    events_processed = body.get('eventsProcessed', 0)
                    result = CommandResult(True, f"Tick completed: {jobs_run} jobs, {events_processed} events")
                else:
                    result = CommandResult(False, error="Failed to run tick")
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

class SignedGroupsCLI(SignedGroupsCore):
    """CLI version of signed groups demo."""
    
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
                        'network_id': self.panels[i].network_id,
                        'channel_id': self.panels[i].channel_id,
                        'messages': self.panels[i].messages,
                        'accessible_channels': self.get_panel_accessible_channels(i)
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
            output.append("SIGNED GROUPS DEMO - FINAL STATE")
            output.append("=" * 80)
            
            # Show 4 panels side by side with their channel sidebars
            output.append("\nPANELS WITH CHANNEL SIDEBARS:")
            output.append("")
            
            # First row of panels (1 & 2)
            output.append(self._format_panel_row([1, 2]))
            output.append("")
            
            # Second row of panels (3 & 4)
            output.append(self._format_panel_row([3, 4]))
            
            # State summary
            output.append("\nSTATE SUMMARY:")
            summary = self.get_state_summary()
            if 'database_tables' in summary:
                output.append("  Database Tables:")
                for table, count in summary['database_tables'].items():
                    output.append(f"    {table}: {count} records")
            else:
                output.append(f"  Identities: {summary['identities']}")
                output.append(f"  Networks: {summary['networks']}")
                output.append(f"  Users: {summary['users']}")
                output.append(f"  Groups: {summary['groups']}")
                output.append(f"  Channels: {summary['channels']}")
            
            # Protocol events (actual event source)
            output.append("\nEVENT SOURCE (newest first):")
            protocol_events = self.get_protocol_events(10)
            if protocol_events:
                for event in protocol_events:
                    payload = event.get('payload', {})
                    event_type = payload.get('type', 'unknown')
                    if event_type == 'message':
                        content = payload.get('content', '')
                        output.append(f"  - {event_type}: {content}")
                    else:
                        output.append(f"  - {event_type}")
            else:
                output.append("  - No protocol events")
            
            # Variables
            if self.variables:
                output.append("\nCAPTURED VARIABLES:")
                for name, value in self.variables.items():
                    output.append(f"  ${name} = {value}")
            
            output.append("=" * 80)
            return '\n'.join(output)
    
    def _format_panel_row(self, panel_nums: List[int]) -> str:
        """Format a row of panels with their sidebars."""
        lines = []
        max_lines = 0
        panel_outputs = {}
        
        # Generate output for each panel
        for panel_num in panel_nums:
            panel_lines = []
            panel = self.panels[panel_num]
            
            # Header
            panel_lines.append(f"PANEL {panel_num}")
            panel_lines.append("-" * 45)
            
            # Identity info
            if panel.identity_name:
                panel_lines.append(f"Identity: {panel.identity_name}")
                
                # Get accessible channels
                channels = self.get_panel_accessible_channels(panel_num)
                
                if channels:
                    panel_lines.append("")
                    panel_lines.append("CHANNELS:")
                    
                    # Group by group
                    channels_by_group = {}
                    for channel in channels:
                        group_name = channel.get('group_name', 'Unknown')
                        if group_name not in channels_by_group:
                            channels_by_group[group_name] = []
                        channels_by_group[group_name].append(channel)
                    
                    for group_name, group_channels in sorted(channels_by_group.items()):
                        # Truncate group name if needed
                        if len(group_name) > 12:
                            group_name = group_name[:10] + ".."
                        panel_lines.append(f"{group_name}:")
                        for channel in sorted(group_channels, key=lambda c: c.get('name', '')):
                            prefix = ">" if channel['id'] == panel.channel_id else " "
                            ch_name = channel.get('name', 'unnamed')
                            # Truncate channel name if needed
                            if len(ch_name) > 13:
                                ch_name = ch_name[:11] + ".."
                            panel_lines.append(f"{prefix} #{ch_name}")
                else:
                    panel_lines.append("")
                    panel_lines.append("No accessible channels")
                
                # Messages
                if panel.messages:
                    panel_lines.append("")
                    panel_lines.append("MESSAGES:")
                    # Show last 5 messages
                    for msg in panel.messages[-5:]:
                        # Truncate long messages
                        if len(msg) > 42:
                            msg = msg[:39] + "..."
                        panel_lines.append(f"  {msg}")
            else:
                panel_lines.append("No identity selected")
                panel_lines.append("Use: /create <name>")
            
            panel_outputs[panel_num] = panel_lines
            max_lines = max(max_lines, len(panel_lines))
        
        # Combine panels side by side
        combined_lines = []
        for i in range(max_lines):
            line_parts = []
            for panel_num in panel_nums:
                panel_lines = panel_outputs[panel_num]
                if i < len(panel_lines):
                    line_parts.append(panel_lines[i].ljust(45))
                else:
                    line_parts.append(" " * 45)
            combined_lines.append(" | ".join(line_parts))
        
        return "\n".join(combined_lines)
    
    def run_script(self, commands: List[str], stop_on_error: bool = False, verbose: bool = False) -> bool:
        """Run a list of commands."""
        success = True
        
        for i, cmd_str in enumerate(commands):
            if verbose:
                print(f"\n[{i+1}/{len(commands)}] Executing: {cmd_str}")
            
            result = self.execute_command(cmd_str)
            
            if verbose or not result.success:
                if result.message:
                    print(f"  Output: {result.message}")
                if result.error:
                    print(f"  ERROR: {result.error}")
            
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
    import asyncio
    
    class HelpModal(ModalScreen[bool]):
        """Modal to display help information."""
        
        CSS = """
        HelpModal {
            align: center middle;
        }
        
        #help-container {
            width: 60;
            height: 24;
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
                yield Label("Signed Groups - Help", id="help-title")
                yield RichLog(id="help-content", wrap=True, markup=True)
                yield Label("Press ESC or ? or click outside to close", id="help-footer")
        
        def on_mount(self) -> None:
            """Display help content when modal is mounted."""
            content = self.query_one("#help-content", RichLog)
            content.write("[bold cyan]Available Commands:[/bold cyan]\n")
            content.write("[green]/create <name>[/green] - Create a new identity")
            content.write("[green]/network <name>[/green] - Create a new network")
            content.write("[green]/invite[/green] - Generate invite code for current network")
            content.write("[green]/join <invite>[/green] - Join network using invite code")
            content.write("[green]/channel <name>[/green] - Create channel in default group")
            content.write("[green]/channel <name> in <group>[/green] - Create channel in specific group")
            content.write("[dim]Click channels in sidebar to switch between channels[/dim]")
            content.write("[green]/group <name>[/green] - Create a new group")
            content.write("[green]/add <user> to <group>[/green] - Add a user to a group")
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
    
    class SignedGroupsDemo(SignedGroupsCore, App):
        """TUI version of signed groups demo."""
        
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
            grid-size: 3 2;
            grid-columns: 1fr 1fr 1fr;
            grid-rows: 1fr 1fr;
            grid-gutter: 1;
        }
        
        LoadingIndicator {
            display: none !important;
        }
        
        #controls {
            column-span: 3;
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
        
        /* Identity panels with integrated channel sidebars */
        #identity-wrapper1, #identity-wrapper2, #identity-wrapper3, #identity-wrapper4 {
            column-span: 1;
            row-span: 1;
            border: solid blue;
            overflow-y: auto;
            layout: horizontal;
        }
        
        .channel-sidebar {
            width: 15%;
            border-right: solid $primary;
            background: $surface-lighten-1;
            overflow-y: auto;
        }
        
        .panel-content {
            width: 85%;
            layout: vertical;
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
        
        .channel-item {
            padding: 0 0 0 1;
            height: auto;
            background: transparent;
            text-align: left;
            width: 100%;
            min-width: 0;
        }
        
        .channel-item:hover {
            background: $boost;
        }
        
        .channel-item.--active {
            background: $primary;
            color: $text;
        }
        
        .channel-group {
            padding: 0 0 0 1;
            margin-top: 0;
            color: $text-muted;
            text-style: bold;
        }
        
        #people-label1, #people-label2, #people-label3, #people-label4,
        #groups-label1, #groups-label2, #groups-label3, #groups-label4 {
            margin-top: 1;
            border-top: solid $primary;
            padding-top: 1;
        }
        
        .person-item {
            padding: 0 0 0 1;
            color: $text;
        }
        
        .group-item {
            padding: 0 0 0 1;
            color: $text-muted;
            text-style: bold;
        }
        
        .group-member {
            padding: 0 0 0 2;
            color: $text;
        }
        """
        
        BINDINGS = [
            ("ctrl+t", "tick", "Tick"),
            ("ctrl+r", "refresh", "Refresh"),
            ("tab", "switch_identity", "Switch Identity"),
            ("q", "quit", "Quit"),
            ("ctrl+c", "quit", "Quit"),
        ]
        
        def __init__(self, db_path='signed_groups_demo.db', reset_db=True):
            # Initialize core logic
            SignedGroupsCore.__init__(self, db_path, reset_db)
            
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
            
            # Grid layout: 3x2
            # Top row: panels 1 and 2, then state inspector
            # Panel 1
            with Container(id="identity-wrapper1"):
                # Channel sidebar for this panel
                with VerticalScroll(classes="channel-sidebar", id="channels-sidebar1"):
                    yield Label("Channels", classes="identity-label")
                    yield Container(id="channels-list1")
                    yield Label("People", classes="identity-label", id="people-label1")
                    yield Container(id="people-list1")
                    yield Label("Groups", classes="identity-label", id="groups-label1")
                    yield Container(id="groups-list1")
                
                # Panel content
                with Vertical(classes="panel-content", id="identity1"):
                    yield Static("Identity 1: None", classes="identity-dropdown", id="identity1-dropdown")
                    yield RichLog(classes="messages", id="messages1", wrap=True, markup=True)
                    yield Input(placeholder="Type message or /help for commands...", id="input1")
            
            # Panel 2
            with Container(id="identity-wrapper2"):
                # Channel sidebar for this panel
                with VerticalScroll(classes="channel-sidebar", id="channels-sidebar2"):
                    yield Label("Channels", classes="identity-label")
                    yield Container(id="channels-list2")
                    yield Label("People", classes="identity-label", id="people-label2")
                    yield Container(id="people-list2")
                    yield Label("Groups", classes="identity-label", id="groups-label2")
                    yield Container(id="groups-list2")
                
                # Panel content
                with Vertical(classes="panel-content", id="identity2"):
                    yield Static("Identity 2: None", classes="identity-dropdown", id="identity2-dropdown")
                    yield RichLog(classes="messages", id="messages2", wrap=True, markup=True)
                    yield Input(placeholder="Type message or /help for commands...", id="input2")
            
            # State inspector (top right)
            with VerticalScroll(id="state-inspector"):
                yield Label("State Inspector", classes="identity-label")
                yield RichLog(id="inspector-log", wrap=True, markup=True)
            
            # Bottom row: panels 3 and 4, then event log
            # Panel 3
            with Container(id="identity-wrapper3"):
                # Channel sidebar for this panel
                with VerticalScroll(classes="channel-sidebar", id="channels-sidebar3"):
                    yield Label("Channels", classes="identity-label")
                    yield Container(id="channels-list3")
                    yield Label("People", classes="identity-label", id="people-label3")
                    yield Container(id="people-list3")
                    yield Label("Groups", classes="identity-label", id="groups-label3")
                    yield Container(id="groups-list3")
                
                # Panel content
                with Vertical(classes="panel-content", id="identity3"):
                    yield Static("Identity 3: None", classes="identity-dropdown", id="identity3-dropdown")
                    yield RichLog(classes="messages", id="messages3", wrap=True, markup=True)
                    yield Input(placeholder="Type message or /help for commands...", id="input3")
            
            # Panel 4
            with Container(id="identity-wrapper4"):
                # Channel sidebar for this panel
                with VerticalScroll(classes="channel-sidebar", id="channels-sidebar4"):
                    yield Label("Channels", classes="identity-label")
                    yield Container(id="channels-list4")
                    yield Label("People", classes="identity-label", id="people-label4")
                    yield Container(id="people-list4")
                    yield Label("Groups", classes="identity-label", id="groups-label4")
                    yield Container(id="groups-list4")
                
                # Panel content
                with Vertical(classes="panel-content", id="identity4"):
                    yield Static("Identity 4: None", classes="identity-dropdown", id="identity4-dropdown")
                    yield RichLog(classes="messages", id="messages4", wrap=True, markup=True)
                    yield Input(placeholder="Type message or /help for commands...", id="input4")
            
            # Event log (bottom right)
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
            
            # Cycle to next identity
            current_idx = self.panel_identity_indices.get(panel_num, -1)
            next_idx = (current_idx + 1) % len(identities)
            self.panel_identity_indices[panel_num] = next_idx
            
            # Update panel state
            identity = identities[next_idx]
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
            invite_display = TextArea(invite_link, read_only=True, id=f"invite-{panel_num}")
            invite_display.styles.height = 3
            invite_display.styles.margin = (1, 0)
            
            # Mount after dropdown
            await identity_container.mount(invite_display, after=f"#identity{panel_num}-dropdown")
            self.invite_displays[old_key] = invite_display
        
        def update_displays(self) -> None:
            """Update all UI displays."""
            self.update_panel_displays()
            self.update_channels_sidebar()
            self.update_people_sidebar()
            self.update_groups_sidebar()
            self.update_state_inspector()
            self.update_event_log()
        
        def update_panel_displays(self) -> None:
            """Update identity panel displays."""
            identities = self.get_identities()
            
            for i in range(1, 5):
                dropdown = self.query_one(f"#identity{i}-dropdown", Static)
                panel = self.panels[i]
                
                # Build context string
                context_parts = [f"Identity {i}:"]
                
                if panel.identity_name:
                    context_parts.append(panel.identity_name)
                    
                    # Check for user
                    user = self.get_user_for_identity(panel.identity_pubkey)
                    if user:
                        context_parts.append(f"User: {user['name']}")
                    
                    # Show channel
                    if panel.channel_id:
                        channels = self.get_channels()
                        channel = next((ch for ch in channels if ch['id'] == panel.channel_id), None)
                        if channel:
                            context_parts.append(f"Ch: #{channel['name']}")
                else:
                    context_parts.append("None")
                
                dropdown.update(" | ".join(context_parts))
                
                # Update messages
                self.update_panel_messages(i)
        
        def update_panel_messages(self, panel_num: int) -> None:
            """Update messages display for a panel."""
            messages_log = self.query_one(f"#messages{panel_num}", RichLog)
            messages_log.clear()
            
            panel = self.panels[panel_num]
            
            if not panel.identity_name:
                messages_log.write("[dim]Use /create <name> to create an identity[/dim]")
            elif not panel.channel_id:
                user = self.get_user_for_identity(panel.identity_pubkey)
                if not user:
                    messages_log.write("[dim]Use /network <name> to create a network[/dim]")
                else:
                    messages_log.write("[dim]Use /channel <name> to create a channel, or click one in the sidebar[/dim]")
            else:
                # Show messages
                identity = self.get_panel_identity(panel_num)
                user = self.get_user_for_identity(identity['pubkey']) if identity else None
                
                for msg in panel.messages[-50:]:  # Last 50 messages
                    # Check if it's our message
                    if user and msg.startswith(f"{user['name']}:"):
                        messages_log.write(f"[bold cyan]{msg}[/bold cyan]")
                    else:
                        messages_log.write(f"[green]{msg}[/green]")
        
        def update_channels_sidebar(self) -> None:
            """Update all channel sidebars."""
            for panel_num in range(1, 5):
                self.update_panel_channels_sidebar(panel_num)
        
        def update_panel_channels_sidebar(self, panel_num: int) -> None:
            """Completely rebuild channels sidebar for a specific panel."""
            try:
                # Get the channels list container directly
                channels_list = self.query_one(f"#channels-list{panel_num}", Container)
                
                # Clear it completely
                for child in list(channels_list.children):
                    child.remove()
                
                # Wait for DOM updates
                self.call_after_refresh(lambda: self._populate_channels(panel_num))
                
            except Exception as e:
                # Log error but don't crash
                print(f"Error updating sidebar for panel {panel_num}: {e}")
        
        def _populate_channels(self, panel_num: int) -> None:
            """Populate the channels for a panel after clearing."""
            try:
                channels_list = self.query_one(f"#channels-list{panel_num}", Container)
                
                # Now populate the container
                identity = self.get_panel_identity(panel_num)
                if not identity:
                    return
                
                user = self.get_user_for_identity(identity['pubkey'])
                if not user:
                    return
                
                # Get accessible channels
                channels = self.get_accessible_channels(user['id'])
                
                # Group channels by group
                channels_by_group = {}
                for channel in channels:
                    group_name = channel.get('group_name', 'Unknown')
                    if group_name not in channels_by_group:
                        channels_by_group[group_name] = []
                    channels_by_group[group_name].append(channel)
                
                # Add widgets synchronously
                for group_name, group_channels in sorted(channels_by_group.items()):
                    # Group header - no ID needed since we're rebuilding everything
                    group_label = Static(f"[bold]{group_name}[/bold]", classes="channel-group")
                    channels_list.mount(group_label)
                    
                    # Add channels
                    for channel in sorted(group_channels, key=lambda c: c.get('name', '')):
                        ch_name = channel.get('name', 'unnamed')
                        # Truncate if too long for narrow sidebar
                        if len(ch_name) > 12:
                            ch_name = ch_name[:10] + ".."
                        
                        # Simple unique ID
                        btn_id = f"ch-{panel_num}-{channel['id']}"
                        channel_btn = Button(
                            f"#{ch_name}",
                            id=btn_id,
                            classes="channel-item"
                        )
                        # Store data for click handling
                        channel_btn.data = {"channel_id": channel['id'], "panel": panel_num}
                        
                        # Mark active channel
                        if self.panels[panel_num].channel_id == channel['id']:
                            channel_btn.add_class("--active")
                        
                        channels_list.mount(channel_btn)
                    
            except Exception as e:
                # Log error but don't crash
                print(f"Error populating channels for panel {panel_num}: {e}")
        
        def on_button_pressed(self, event: Button.Pressed) -> None:
            """Handle all button presses including channel selection."""
            button_id = event.button.id
            
            # Check if it's a channel button by data attribute
            if hasattr(event.button, 'data') and event.button.data and 'channel_id' in event.button.data:
                panel_num = event.button.data['panel']
                channel_id = event.button.data['channel_id']
                self.select_channel(panel_num, channel_id)
                return
            
            # Original button handling
            if button_id == "play-pause-btn":
                self.toggle_play_pause()
            elif button_id == "tick-btn":
                self.action_tick()
            elif button_id == "refresh-btn":
                self.action_refresh()
        
        def select_channel(self, panel_num: int, channel_id: str) -> None:
            """Select a channel for a specific panel."""
            # Set channel
            self.panels[panel_num].channel_id = channel_id
            
            # Refresh displays
            self.refresh_panel_messages(panel_num)
            self.update_displays()
        
        def update_people_sidebar(self) -> None:
            """Update all people sidebars."""
            for panel_num in range(1, 5):
                self.update_panel_people_sidebar(panel_num)
        
        def update_panel_people_sidebar(self, panel_num: int) -> None:
            """Update the people list for a specific panel."""
            try:
                people_list = self.query_one(f"#people-list{panel_num}", Container)
                
                # Clear it completely
                for child in list(people_list.children):
                    child.remove()
                
                # Get current identity for this panel
                identity = self.get_panel_identity(panel_num)
                if not identity:
                    return
                
                user = self.get_user_for_identity(identity['pubkey'])
                if not user or not user.get('network_id'):
                    return
                
                # Get all users in the same network
                network_users = self.get_users(user['network_id'])
                
                # Add each person
                for network_user in sorted(network_users, key=lambda u: u.get('name', '')):
                    person_name = network_user.get('name', 'Unknown')
                    person_label = Static(person_name, classes="person-item")
                    people_list.mount(person_label)
                    
            except Exception as e:
                print(f"Error updating people sidebar for panel {panel_num}: {e}")
        
        def update_groups_sidebar(self) -> None:
            """Update all groups sidebars."""
            for panel_num in range(1, 5):
                self.update_panel_groups_sidebar(panel_num)
        
        def update_panel_groups_sidebar(self, panel_num: int) -> None:
            """Update the groups list for a specific panel."""
            try:
                groups_list = self.query_one(f"#groups-list{panel_num}", Container)
                
                # Clear it completely
                for child in list(groups_list.children):
                    child.remove()
                
                # Get current identity for this panel
                identity = self.get_panel_identity(panel_num)
                if not identity:
                    return
                
                user = self.get_user_for_identity(identity['pubkey'])
                if not user or not user.get('network_id'):
                    return
                
                # Get all groups (they don't have network_id stored)
                all_groups = self.get_groups()
                
                # Filter groups by checking if they have channels in this network
                network_channels = self.get_channels(user['network_id'])
                network_group_ids = set(ch.get('group_id') for ch in network_channels if ch.get('group_id'))
                
                # Also include groups created by users in this network
                network_users = self.get_users(user['network_id'])
                for u in network_users:
                    for g in all_groups:
                        if g.get('created_by') == u['id']:
                            network_group_ids.add(g['id'])
                
                network_groups = [g for g in all_groups if g['id'] in network_group_ids]
                
                # Get all adds (group memberships) 
                adds = self._cache.get('adds', [])
                
                # Build groups with their members
                for group in sorted(network_groups, key=lambda g: g.get('name', '')):
                    group_name = group.get('name', 'Unknown')
                    group_label = Static(f"[bold]{group_name}[/bold]", classes="group-item")
                    groups_list.mount(group_label)
                    
                    # Find members of this group
                    group_members = []
                    
                    # Check who created the group (they're automatically a member)
                    if group.get('created_by'):
                        creator = next((u for u in self.get_users() if u['id'] == group['created_by']), None)
                        if creator:
                            group_members.append(creator)
                    
                    # Find users added to this group
                    for add in adds:
                        if add['group_id'] == group['id']:
                            member = next((u for u in self.get_users() if u['id'] == add['user_id']), None)
                            if member and member not in group_members:
                                group_members.append(member)
                    
                    # Display members
                    for member in sorted(group_members, key=lambda m: m.get('name', '')):
                        member_name = member.get('name', 'Unknown')
                        member_label = Static(member_name, classes="group-member")
                        groups_list.mount(member_label)
                    
            except Exception as e:
                print(f"Error updating groups sidebar for panel {panel_num}: {e}")
        
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
            
            # Show protocol events (actual event source)
            protocol_events = self.get_protocol_events(20)
            if not protocol_events:
                event_log.write("[dim]No events yet. Execute commands to see them here.[/dim]")
                return
                
            for i, event in enumerate(protocol_events):
                # Extract payload and metadata
                payload = event.get('payload', {})
                metadata = event.get('metadata', {})
                
                # Format event header
                event_type = payload.get('type', 'unknown')
                event_id = metadata.get('eventId', f'event-{i}')
                event_log.write(Text(f"\n[Event: {event_id[:8]}...] Type: {event_type}", style="bold cyan"))
                
                # Show key payload fields based on event type
                if event_type == 'message':
                    event_log.write(f"  Channel: {payload.get('channel_id', 'N/A')[:8]}...")
                    event_log.write(f"  Author: {payload.get('author_id', 'N/A')[:8]}...")
                    event_log.write(f"  Content: {payload.get('content', '')}")
                elif event_type == 'identity':
                    event_log.write(f"  Name: {payload.get('name', 'N/A')}")
                    event_log.write(f"  Pubkey: {payload.get('pubkey', 'N/A')[:16]}...")
                elif event_type == 'network':
                    event_log.write(f"  Name: {payload.get('name', 'N/A')}")
                    event_log.write(f"  ID: {payload.get('id', 'N/A')[:8]}...")
                elif event_type == 'user':
                    event_log.write(f"  Name: {payload.get('name', 'N/A')}")
                    event_log.write(f"  Network: {payload.get('network_id', 'N/A')[:8]}...")
                elif event_type == 'channel':
                    event_log.write(f"  Name: {payload.get('name', 'N/A')}")
                    event_log.write(f"  ID: {payload.get('id', 'N/A')[:8]}...")
                else:
                    # Show first few fields of payload for unknown types
                    for key, value in list(payload.items())[:3]:
                        if isinstance(value, str) and len(value) > 50:
                            value = value[:47] + "..."
                        event_log.write(f"  {key}: {value}")


# ============================================================================
# Main Entry Point
# ============================================================================

def run_cli_mode(args):
    """Run in CLI scripting mode."""
    cli = SignedGroupsCLI(db_path=args.db_path, reset_db=not args.no_reset)
    
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
    
    return 0 if success else 1


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Signed Groups Demo (API Version)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
TUI Mode (default):
  %(prog)s                           # Run interactive TUI
  %(prog)s --no-reset                # Keep existing database
  
CLI Script Mode:
  %(prog)s --run "1:/create alice" "1:/network fun" "2:/create bob"
  %(prog)s --run "1:/invite -> link" "2:/join $link"
  %(prog)s --script-file demo.script
  %(prog)s --run "1:/create alice" --format json --verbose
"""
    )
    
    # Common options
    parser.add_argument('--no-reset', action='store_true', 
                        help='Do not reset database on startup')
    parser.add_argument('--db-path', default='signed_groups_demo.db',
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
        # CLI mode
        sys.exit(run_cli_mode(args))
    else:
        # TUI mode
        app = SignedGroupsDemo(db_path=args.db_path, reset_db=not args.no_reset)
        app.run()


if __name__ == "__main__":
    main()