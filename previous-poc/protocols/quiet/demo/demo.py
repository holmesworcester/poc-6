#!/usr/bin/env python3
"""
Quiet Protocol Demo - Unified CLI and TUI interface.

This demo shows:
- Creating identities 
- Managing channels and groups (UI only for now)
- Database state visualization
- Protocol event logging

Following poc-3 pattern: same business logic for both CLI and TUI modes.
"""

import json
import sys
import os
import asyncio
import subprocess
import signal
import time
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from enum import Enum

# Add project root to path
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from core.api import API, APIClient, APIError  # APIClient is alias for backward compatibility
from protocols.quiet import client as qapi

# For CLI testing mode
import argparse

# Conditional Textual imports (only needed for TUI mode)
try:
    from textual.app import App, ComposeResult
    from textual.widgets import (
        Header, Footer, Button, Static, Input, Log, DataTable, Label, RichLog, Tree, SelectionList
    )
    # Optional TextLog (newer Textual). If unavailable, we will fallback to RichLog.
    try:
        from textual.widgets import TextLog  # type: ignore
        HAVE_TEXTLOG = True
    except Exception:
        HAVE_TEXTLOG = False
    from textual.widgets.selection_list import Selection
    from textual.containers import Container, Horizontal, Vertical, Grid, VerticalScroll, ScrollableContainer
    from textual.reactive import reactive
    from textual import on, events
    from textual.message import Message
    from textual.binding import Binding
    from textual.screen import ModalScreen
    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False
    HAVE_TEXTLOG = False


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
    """State for a single identity panel."""
    identity_id: Optional[str] = None
    identity_name: Optional[str] = None
    network_id: Optional[str] = None  # Track actual network ID
    peer_id: Optional[str] = None
    current_channel: Optional[str] = None
    current_group: Optional[str] = None
    messages: List[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.messages is None:
            self.messages = []


# ============================================================================
# Core Business Logic (No UI Dependencies)
# ============================================================================

class QuietDemoCore:
    """Core business logic for Quiet demo. No UI dependencies."""
    
    def __init__(self, reset_db: bool = True):
        # Use direct API client (no HTTP) with explicit protocol directory
        protocol_dir = Path(__file__).parent.parent  # protocols/quiet
        self.api = APIClient(protocol_dir=protocol_dir, reset_db=reset_db)
        
        # Panel states (1-4) 
        self.panels = {i: PanelState() for i in range(1, 5)}
        
        # Initialize empty data - will be populated from database
        self.channels = {}  # channel_id -> {"name": str, "group": str, "description": str}
        self.groups = {}    # group_id -> {"name": str, "members": []}
        self.people = {}    # identity_id -> {"name": str, "online": bool}
        
        # Cache for state
        self._cache = {}
        self._cache_timestamp = 0
        
        # Event log
        self.events = []
        self.event_counter = 0
        
        # Command history
        self.command_history = []
        
        # Variables for CLI mode
        self.variables = {}
        
        # Current panel for CLI mode (tracks which panel is active)
        self.current_cli_panel = 1
        
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
            # Get all entities via API operations only
            try:
                self._cache['identities'] = qapi.core_identity_list(self.api)['data']['identities']
            except:
                self._cache['identities'] = []

            try:
                self._cache['transit_keys'] = qapi.transit_key_list(self.api)
            except:
                self._cache['transit_keys'] = []

            try:
                self._cache['keys'] = qapi.key_list(self.api)
            except:
                self._cache['keys'] = []

            # Get groups and channels via API operations
            # Use the first identity or current panel's identity for queries
            query_identity = None
            # Check if we have an active panel (TUI mode)
            if hasattr(self, 'active_panel_id') and self.active_panel_id in self.panels:
                query_identity = self.panels[self.active_panel_id].identity_id
            # Otherwise use first panel with an identity (CLI mode)
            if not query_identity:
                for panel in self.panels.values():
                    if panel.identity_id:
                        query_identity = panel.identity_id
                        break
            # Fall back to first identity in cache
            if not query_identity and self._cache['identities']:
                query_identity = self._cache['identities'][0]['identity_id']

            # NOTE: This method is for global cache refresh. Panel-specific data
            # should be fetched using panel-specific methods (fetch_groups_direct, etc.)
            # We'll keep groups/channels/users empty here to avoid cross-panel pollution
            self._cache['groups'] = []
            self._cache['channels'] = []
            self._cache['users'] = []
            
            # Clear global collections - panels should use panel-specific fetch methods
            # This prevents cross-panel data pollution
            self.people = {}
            self.groups = {}
            self.channels = {}
            
            self._cache_timestamp = time.time()
        except Exception as e:
            # Don't reset the entire cache on error - just log it
            print(f"Warning: Error refreshing state: {e}")
            # Initialize cache if it doesn't exist
            if not hasattr(self, '_cache'):
                self._cache = {
                    'identities': [],
                    'transit_keys': [],
                    'keys': [],
                    'groups': [],
                    'channels': []
                }
    
    def get_identities(self) -> List[Dict[str, Any]]:
        """Get cached identities."""
        return self._cache.get('identities', [])
    
    def get_groups(self) -> List[Dict[str, Any]]:
        """Get cached groups."""
        return self._cache.get('groups', [])

    def get_channels(self) -> List[Dict[str, Any]]:
        """Get cached channels."""
        return self._cache.get('channels', [])

    def fetch_messages(self, identity_id: str, channel_id: str) -> List[Dict[str, Any]]:
        """Fetch messages from database for a channel."""
        try:
            if not identity_id or not channel_id:
                return []

            return qapi.message_get(self.api, {
                'identity_id': identity_id,
                'channel_id': channel_id,
                'limit': 50
            })
        except Exception as e:
            print(f"Error fetching messages: {e}")
            return []

    def fetch_groups_direct(self, identity_id: str, network_id: str) -> List[Dict[str, Any]]:
        """Fetch groups for a specific identity and network (panel-scoped)."""
        try:
            if not identity_id or not network_id:
                return []
            result = qapi.group_get(self.api, {
                'identity_id': identity_id,
                'network_id': network_id
            })
            return result if result else []
        except Exception as e:
            print(f"Error fetching groups for identity {identity_id}, network {network_id}: {e}")
            return []

    def fetch_channels_direct(self, identity_id: str, group_id: str | None = None, network_id: str | None = None) -> List[Dict[str, Any]]:
        """Fetch channels for a specific identity by group or network (panel-scoped)."""
        try:
            if not identity_id:
                return []
            params: Dict[str, Any] = {'identity_id': identity_id}
            if group_id:
                params['group_id'] = group_id
            if network_id:
                params['network_id'] = network_id
            return qapi.channel_get(self.api, params)
        except Exception as e:
            print(f"Error fetching channels: {e}")
            return []
    def fetch_users(self, identity_id: str, network_id: str) -> List[Dict[str, Any]]:
        """Fetch users from database for a network."""
        try:
            if not identity_id or not network_id:
                return []

            return qapi.user_get(self.api, {
                'identity_id': identity_id,
                'network_id': network_id
            })
        except Exception as e:
            print(f"Error fetching users: {e}")
            return []

    def get_channel_info(self, panel_id: int, channel_id: str) -> Optional[Dict[str, Any]]:
        """Get channel info for a specific panel and channel_id."""
        panel = self.panels.get(panel_id)
        if not panel or not panel.identity_id:
            return None

        # First try to fetch all channels for this panel's network
        if panel.network_id:
            channels = self.fetch_channels_direct(panel.identity_id, network_id=panel.network_id)
            for ch in channels:
                if ch['channel_id'] == channel_id:
                    return {
                        "name": ch['name'],
                        "group": ch.get('group_id'),
                        "description": ch.get('description', '')
                    }
        return None

    def get_panel_state(self, panel_id: int) -> PanelState:
        """Get state for a specific panel."""
        return self.panels.get(panel_id, PanelState())
    
    # ========================================================================
    # Event Logging
    # ========================================================================
    
    def _add_event(self, event_type: str, message: str, details: Optional[Dict] = None):
        """Add an event to the log."""
        self.event_counter += 1
        event = {
            'id': self.event_counter,
            'type': event_type,
            'message': message,
            'timestamp': datetime.now().isoformat(),
            'details': details or {}
        }
        self.events.append(event)
        
        # Keep only last 1000 events
        if len(self.events) > 1000:
            self.events = self.events[-1000:]
    
    # ========================================================================
    # Commands
    # ========================================================================
    
    def create_identity(self, panel_id: int, name: Optional[str] = None) -> CommandResult:
        """Create a new identity for a panel."""
        panel = self.panels.get(panel_id)
        if not panel:
            return CommandResult(False, error="Invalid panel ID")

        # Check if panel already has an identity
        if panel.identity_id:
            return CommandResult(False, error="This panel already has an identity. Use a different panel.")

        try:
            username = name or f'User-{panel_id}'

            # First create core identity (core framework function)
            result = qapi.core_identity_create(self.api, {'name': username})

            if not result or 'ids' not in result or 'identity' not in result['ids']:
                return CommandResult(False, error="Failed to create identity")

            identity_id = result['ids']['identity']

            # Now create a peer for this identity (peer represents identity in protocol)
            peer_result = qapi.create_peer(self.api, {
                'identity_id': identity_id,
                'username': username
            })

            if not peer_result or 'ids' not in peer_result or 'peer' not in peer_result['ids']:
                return CommandResult(False, error="Failed to create peer")

            peer_id = peer_result['ids']['peer']

            # Update panel with both identity and peer info
            panel.identity_id = identity_id
            panel.peer_id = peer_id
            panel.identity_name = username

            # Refresh state to update UI
            self.refresh_state(force=True)

            self._add_event('command', f"Panel {panel_id}: Created identity '{panel.identity_name}'")
            return CommandResult(True,
                message=f"Created identity '{panel.identity_name}' (ID: {identity_id[:8]}...)",
                data={'identity_id': identity_id, 'peer_id': peer_id})

        except APIError as e:
            self._add_event('error', f"Panel {panel_id}: Failed to create identity", {'error': str(e)})
            return CommandResult(False, error=str(e))
    
    def create_network(self, panel_id: int, name: str) -> CommandResult:
        """Create a new network with default channel."""
        panel = self.panels.get(panel_id)
        if not panel:
            return CommandResult(False, error="Invalid panel ID")

        # Panel must have a peer to create a network (peer-first architecture)
        if not hasattr(panel, 'peer_id') or not panel.peer_id:
            return CommandResult(False, error="Panel has no peer. Create an identity first with /create <name>")

        try:
            # Create network using the peer_id (networks depend on peers)
            result = qapi.create_network(self.api, {
                'name': name,
                'peer_id': panel.peer_id
            })

            if not result or "ids" not in result:
                return CommandResult(False, error="Failed to create network")

            network_id = result["ids"].get("network")
            if not network_id:
                return CommandResult(False, error="Network creation failed: No network ID returned")

            panel.network_id = network_id

            # Create user event to join the network
            user_result = qapi.create_user(self.api, {
                'peer_id': panel.peer_id,
                'network_id': network_id,
                'name': panel.identity_name
            })

            # Create default group
            group_result = qapi.create_group(self.api, {
                'name': 'public',
                'network_id': network_id,
                'peer_id': panel.peer_id
            })

            if group_result and "ids" in group_result:
                group_id = group_result["ids"].get("group")
                if group_id:
                    panel.current_group = group_id

                    # Create default channel in the group
                    channel_result = qapi.create_channel(self.api, {
                        'name': 'general',
                        'group_id': group_id,
                        'peer_id': panel.peer_id,
                        'network_id': network_id
                    })

                    if channel_result and "ids" in channel_result:
                        channel_id = channel_result["ids"].get("channel")
                        if channel_id:
                            panel.current_channel = channel_id

            # Refresh state to load the new channels
            self.refresh_state(force=True)

            self._add_event('command', f"Panel {panel_id}: Created network '{name}'")
            return CommandResult(True, f"Created network: {name}", data={"network_id": network_id})

        except APIError as e:
            self._add_event('error', f"Panel {panel_id}: Failed to create network", {'error': str(e)})
            return CommandResult(False, error=str(e))
    
    def generate_invite(self, panel_id: int) -> CommandResult:
        """Generate an invite link for the current network."""
        panel = self.panels.get(panel_id)
        if not panel or not panel.identity_id:
            return CommandResult(False, error="No identity selected")

        if not panel.network_id:
            return CommandResult(False, error="No network selected")

        if not hasattr(panel, 'peer_id') or not panel.peer_id:
            return CommandResult(False, error="Panel has no peer")

        try:
            # Get the first group if no current group is set
            if not panel.current_group:
                # Refresh to get groups
                self.refresh_state(force=True)
                if self._cache.get('groups'):
                    # Use the first group
                    panel.current_group = self._cache['groups'][0]['group_id']

            # Execute the create_invite operation through the API
            result = qapi.create_invite(self.api, {
                'network_id': panel.network_id,
                'peer_id': panel.peer_id,
                'group_id': panel.current_group or ''
            })

            # The result should contain the invite_link
            if result and "ids" in result:
                # Extract invite link from the result data
                if "data" in result and "invite_link" in result["data"]:
                    invite_link = result["data"]["invite_link"]
                else:
                    # Fallback - generate simple invite code
                    invite_link = f"invite_{result['ids'].get('invite', 'unknown')[:8]}"

                # Log full link to the Log panel for easy copy
                self._add_event('success', f"Panel {panel_id}: Invite generated -> {invite_link}")
                return CommandResult(True, f"Invite code: {invite_link}", data={"invite": invite_link})
            else:
                return CommandResult(False, error="Failed to generate invite")

        except APIError as e:
            self._add_event('error', f"Panel {panel_id}: Failed to generate invite", {'error': str(e)})
            return CommandResult(False, error=str(e))
    
    def join_network_with_invite(self, panel_id: int, invite_code: str, name: Optional[str] = None) -> CommandResult:
        """Join a network using an invite code."""
        panel = self.panels.get(panel_id)
        if not panel:
            return CommandResult(False, error="Invalid panel ID")

        # Check if panel already has an identity
        if panel.identity_id:
            return CommandResult(False, error="This panel already has an identity. Use a different panel.")

        try:
            username = name or panel.identity_name or f"User-{panel_id}"

            # Use join_as_user which creates identity, peer, and user all at once
            result = qapi.join_as_user(self.api, {
                'invite_link': invite_code,
                'name': username
            })

            if not result or "ids" not in result:
                return CommandResult(False, error=result.get("error", "Failed to join network"))

            # Extract the created IDs
            identity_id = result["ids"].get("identity")
            peer_id = result["ids"].get("peer")
            user_id = result["ids"].get("user")

            if not identity_id or not peer_id:
                return CommandResult(False, error="Failed to join network: Missing identity or peer")

            # Update panel with identity and peer info
            panel.identity_id = identity_id
            panel.peer_id = peer_id
            panel.identity_name = username

            # Parse invite to get network_id
            if invite_code.startswith("quiet://invite/"):
                import base64
                import json
                try:
                    invite_b64 = invite_code[15:]  # Remove prefix
                    invite_json = base64.b64decode(invite_b64).decode()
                    invite_data = json.loads(invite_json)
                    network_id = invite_data.get('network_id')
                    if network_id:
                        panel.network_id = network_id
                except:
                    pass  # If parsing fails, we'll get network_id from refresh

            # Refresh state to get channels and groups
            self.refresh_state(force=True)

            # Find and set the first channel in the network
            if panel.network_id:
                channels = self.fetch_channels_direct(panel.identity_id, network_id=panel.network_id)
                if channels:
                    panel.current_channel = channels[0]['channel_id']

            self._add_event('command', f"Panel {panel_id}: Joined network with invite code")
            return CommandResult(True, f"Joined network as: {panel.identity_name}")

        except APIError as e:
            self._add_event('error', f"Panel {panel_id}: Failed to join network", {'error': str(e)})
            return CommandResult(False, error=str(e))
    
    def join_channel(self, panel_id: int, channel_name_or_id: str) -> CommandResult:
        """Join a channel by name or ID."""
        panel = self.panels.get(panel_id)
        if not panel:
            return CommandResult(False, error="Invalid panel ID")

        if not panel.identity_id:
            return CommandResult(False, error="No identity selected")

        # Get channels for this panel's network
        if not panel.network_id:
            return CommandResult(False, error="No network selected")

        channels = self.fetch_channels_direct(panel.identity_id, network_id=panel.network_id)

        # Find channel by name or ID
        channel_id = None
        channel_name = None

        for ch in channels:
            if ch['channel_id'] == channel_name_or_id or ch['name'] == channel_name_or_id:
                channel_id = ch['channel_id']
                channel_name = ch['name']
                break

        if not channel_id:
            return CommandResult(False, error=f"Channel '{channel_name_or_id}' does not exist in this network")

        panel.current_channel = channel_id
        panel.messages.append({
            "type": "system",
            "text": f"Joined #{channel_name}",
            "timestamp": datetime.now()
        })

        self._add_event('command', f"Panel {panel_id}: Joined channel '{channel_name}'")
        return CommandResult(True, f"Joined #{channel_name}")
    
    def create_group(self, panel_id: int, name: str) -> CommandResult:
        """Create a new group with default channel."""
        panel = self.panels.get(panel_id)
        if not panel or not panel.identity_id:
            return CommandResult(False, error="No identity selected")

        if not panel.network_id:
            return CommandResult(False, error="No network selected. Create a network first with /network <name>")

        try:
            # Create the group
            result = qapi.create_group(self.api, {
                'name': name,
                'network_id': panel.network_id,
                'peer_id': panel.identity_id  # demo uses identity_id here; peer-first may expect peer_id
            })

            # Group creation returns group_id directly, not in ids
            group_id = None
            if result:
                if "ids" in result:
                    group_id = result["ids"].get("group")
                elif "group_id" in result:
                    group_id = result["group_id"]

                if group_id:
                    self.refresh_state(force=True)

                    # Create default channel in the group
                    channel_result = qapi.create_channel(self.api, {
                        'name': 'general',
                        'group_id': group_id,
                        'peer_id': panel.identity_id,
                        'network_id': panel.network_id
                    })

                    if channel_result and "ids" in channel_result:
                        self.refresh_state(force=True)
                        self._add_event('command', f"Panel {panel_id}: Created group '{name}' with default channel")
                        return CommandResult(True, f"Created group: {name} with #general channel (ID: {group_id[:16]}...)")
                    else:
                        self._add_event('command', f"Panel {panel_id}: Created group '{name}' (no default channel)")
                        return CommandResult(True, f"Created group: {name} (ID: {group_id[:16]}...)")
                else:
                    return CommandResult(False, error="Group creation failed: No group ID returned")
            else:
                return CommandResult(False, error="Failed to create group")
                
        except APIError as e:
            self._add_event('error', f"Panel {panel_id}: Failed to create group", {'error': str(e)})
            return CommandResult(False, error=str(e))
    
    def create_channel(self, panel_id: int, group_id: str, name: str) -> CommandResult:
        """Create a new channel in a group."""
        panel = self.panels.get(panel_id)
        if not panel or not panel.identity_id:
            return CommandResult(False, error="No identity selected")

        if not panel.network_id:
            return CommandResult(False, error="No network selected")

        try:
            result = qapi.create_channel(self.api, {
                'name': name,
                'group_id': group_id,
                'peer_id': panel.identity_id,
                'network_id': panel.network_id
            })

            if result and "ids" in result:
                channel_id = result["ids"].get("channel")
                if channel_id:
                    self.refresh_state(force=True)
                    self._add_event('command', f"Panel {panel_id}: Created channel '{name}' in group")
                    return CommandResult(True, f"Created channel: #{name}")
                else:
                    return CommandResult(False, error="Channel creation failed: No channel ID returned")
            else:
                return CommandResult(False, error="Failed to create channel")
                
        except APIError as e:
            self._add_event('error', f"Panel {panel_id}: Failed to create channel", {'error': str(e)})
            return CommandResult(False, error=str(e))
    
    def send_message(self, panel_id: int, text: str, channel_id: str = None) -> CommandResult:
        """Send a message to a channel."""
        panel = self.panels.get(panel_id)
        if not panel:
            return CommandResult(False, error="Invalid panel ID")

        if not panel.identity_id:
            return CommandResult(False, error="No identity selected")

        # Use provided channel_id or current channel
        target_channel = channel_id or panel.current_channel
        if not target_channel:
            return CommandResult(False, error="No channel specified")

        try:
            result = qapi.create_message(self.api, {
                'content': text,
                'channel_id': target_channel,
                'peer_id': panel.peer_id
            })

            if result and "ids" in result:
                message_id = result["ids"].get("message")
                if message_id:
                    # Add message to panel display
                    panel.messages.append({
                        "type": "message",
                        "from": panel.identity_name,
                        "text": text,
                        "channel": panel.current_channel,
                        "timestamp": datetime.now()
                    })

                    self._add_event('message', f"{panel.identity_name}: {text[:50]}...")
                    return CommandResult(True)
                else:
                    return CommandResult(False, error="Message creation failed: No message ID returned")
            else:
                return CommandResult(False, error="Failed to send message")
                
        except APIError as e:
            self._add_event('error', f"Panel {panel_id}: Failed to send message", {'error': str(e)})
            return CommandResult(False, error=str(e))
    
    # ========================================================================
    # CLI Command Interface
    # ========================================================================
    
    def execute_cli_command(self, command: str) -> str:
        """Execute a CLI command and return the output."""
        self.command_history.append(command)
        
        # Support for variable capture (command -> var)
        capture_var = None
        if " -> " in command:
            command, capture_var = command.rsplit(" -> ", 1)
            capture_var = capture_var.strip()
        
        # Support for variable substitution
        for var_name, var_value in self.variables.items():
            command = command.replace(f"${var_name}", var_value)
            command = command.replace(f"${{{var_name}}}", var_value)
        
        parts = command.strip().split()
        
        if not parts:
            return ""
        
        cmd = parts[0].lower()
        
        try:
            result_data = None
            output = ""
            
            if cmd == "help":
                output = self._cli_help()
            
            elif cmd == "/create" and len(parts) > 1:
                # Panel-scoped: /create <name> creates identity in active panel
                name = " ".join(parts[1:])
                panel_id = self.current_cli_panel
                if self.panels[panel_id].identity_id:
                    output = f"Error: Panel {panel_id} already has an identity. Use /switch to an empty panel."
                else:
                    result = self.create_identity(panel_id, name)
                    if result.success:
                        self.current_cli_panel = panel_id
                    output = result.message if result.success else f"Error: {result.error}"
            
            elif cmd == "/network" and len(parts) > 1:
                # Create network command
                network_name = " ".join(parts[1:])
                # Use current panel if it has an identity
                panel_id = self.current_cli_panel

                result = self.create_network(panel_id, network_name)
                if result.success:
                    result_data = result.data
                output = result.message if result.success else f"Error: {result.error}"
            
            elif cmd == "/invite":
                # Generate invite for current panel
                if self.panels[self.current_cli_panel].identity_id:
                    result = self.generate_invite(self.current_cli_panel)
                    if result.success:
                        result_data = result.data
                    output = result.message if result.success else f"Error: {result.error}"
                else:
                    output = f"Error: Panel {self.current_cli_panel} has no identity"
            
            elif cmd == "/join" and len(parts) > 1:
                # Panel-scoped: join in the active empty panel
                # Supports formats: /join <invite>  OR  /join <name> <invite>
                args = parts[1:]
                if len(args) == 1:
                    invite_code = args[0]
                    chosen_name = None
                else:
                    invite_code = args[-1]
                    chosen_name = " ".join(args[:-1]).strip() or None
                panel_id = self.current_cli_panel
                if self.panels[panel_id].identity_id:
                    output = f"Error: Panel {panel_id} already has an identity. Use /switch to an empty panel."
                else:
                    result = self.join_network_with_invite(panel_id, invite_code, chosen_name)
                    if result.success:
                        self.current_cli_panel = panel_id
                    output = result.message if result.success else f"Error: {result.error}"
            
            elif cmd == "/group" and len(parts) > 1:
                group_name = " ".join(parts[1:])
                # Use the current CLI panel
                if self.panels[self.current_cli_panel].identity_id:
                    result = self.create_group(self.current_cli_panel, group_name)
                    output = result.message if result.success else f"Error: {result.error}"
                else:
                    output = f"Error: Panel {self.current_cli_panel} has no identity. Create one first with /create <name>"
            
            elif cmd == "/channel" and len(parts) > 2:
                # Format: /channel <group_id> <channel_name>
                group_id = parts[1]
                channel_name = " ".join(parts[2:])
                if self.panels[self.current_cli_panel].identity_id:
                    result = self.create_channel(self.current_cli_panel, group_id, channel_name)
                    if result.success:
                        # Auto-join the channel
                        self.panels[self.current_cli_panel].current_channel = channel_name
                    output = result.message if result.success else f"Error: {result.error}"
                else:
                    output = f"Error: Panel {self.current_cli_panel} has no identity. Create one first with /create <name>"
            
            elif cmd in ("panel", "/panel") and len(parts) > 1:
                panel_id = int(parts[1])
                panel = self.panels.get(panel_id)
                if not panel:
                    output = f"Invalid panel ID: {panel_id}"
                else:
                    lines = [f"Panel {panel_id}:"]
                    if panel.identity_name:
                        lines.append(f"  Identity: {panel.identity_name}")
                        lines.append(f"  ID: {panel.identity_id[:16]}...")
                        lines.append(f"  Network: {panel.network_id}")
                        if panel.current_channel:
                            # Get channel name from panel's network
                            ch_info = self.get_channel_info(panel_id, panel.current_channel)
                            if ch_info:
                                channel_name = ch_info['name']
                                lines.append(f"  Channel: #{channel_name}")
                            else:
                                lines.append(f"  Channel: #{panel.current_channel}")
                        if panel_id == self.current_cli_panel:
                            lines.append("  [ACTIVE]")
                    else:
                        lines.append("  No identity")
                    output = "\n".join(lines)
            
            elif cmd in ("switch", "/switch") and len(parts) > 1:
                panel_id = int(parts[1])
                if panel_id < 1 or panel_id > 4:
                    output = "Error: Panel ID must be between 1 and 4"
                else:
                    self.current_cli_panel = panel_id
                    panel = self.panels[panel_id]
                    if panel.identity_name:
                        output = f"Switched to panel {panel_id} (Identity: {panel.identity_name})"
                    else:
                        output = f"Switched to panel {panel_id} (No identity)"
            
            elif cmd == "db":
                # Get database state via API
                db_dump = self.api.dump_database()
                lines = ["Database State:"]
                for table_name, rows in sorted(db_dump.items()):
                    lines.append(f"\n{table_name}: {len(rows)} rows")
                    if rows and len(rows) <= 3:
                        for i, row in enumerate(rows):
                            lines.append(f"  [{i}] {self._format_row(row)}")
                output = "\n".join(lines)
            
            elif cmd == "groups":
                try:
                    # Query groups via API
                    self.refresh_state(force=True)
                    groups = self.get_groups()
                    if not groups:
                        output = "No groups found"
                    else:
                        lines = ["Groups:"]
                        for group in groups:
                            lines.append(f"  [{group['group_id'][:16]}...] {group['name']}")
                        output = "\n".join(lines)
                except Exception as e:
                    output = f"Error listing groups: {e}"
            
            elif cmd == "events":
                limit = int(parts[1]) if len(parts) > 1 else 10
                recent_events = self.events[-limit:]
                if not recent_events:
                    output = "No events"
                else:
                    lines = [f"Recent events (last {limit}):"]
                    for event in recent_events:
                        lines.append(f"  [{event['id']}] {event['type']}: {event['message']}")
                    output = "\n".join(lines)

            elif cmd == "sidebar":
                # Show panel-scoped sidebar: groups, channels, people
                panel = self.panels[self.current_cli_panel]
                if not panel.identity_id:
                    output = f"Panel {self.current_cli_panel} has no identity"
                else:
                    lines: list[str] = [f"Sidebar (panel {self.current_cli_panel}):"]
                    # Groups
                    groups = []
                    if panel.network_id:
                        groups = self.fetch_groups_direct(panel.identity_id, panel.network_id)
                    lines.append("Groups:")
                    if not groups:
                        lines.append("  (none)")
                    else:
                        for g in groups:
                            lines.append(f"  [{g['group_id'][:8]}] {g['name']}")
                            # Channels for each group
                            chans = self.fetch_channels_direct(panel.identity_id, group_id=g['group_id'])
                            if chans:
                                for ch in chans:
                                    lines.append(f"    # {ch['name']} ({ch['channel_id'][:8]})")
                    # People/users in network
                    users = []
                    if panel.network_id:
                        users = self.fetch_users(panel.identity_id, panel.network_id)
                    lines.append("People:")
                    if not users:
                        lines.append("  (none)")
                    else:
                        for u in users:
                            uname = u.get('username') or u.get('name') or u.get('user_id', '')[:8]
                            lines.append(f"  - {uname}")
                    output = "\n".join(lines)

            elif cmd == "log":
                # Dump core events as the TUI Log panel would
                if not self.events:
                    output = "Log is empty"
                else:
                    lines = ["Log (most recent first):"]
                    for ev in self.events:
                        lines.append(f"  {ev.get('timestamp','')} - {ev.get('message','')}")
                    output = "\n".join(lines)

            elif cmd == "snapshot":
                # Print panel sidebars + recent messages for all panels
                lines: list[str] = ["=== Snapshot ==="]
                for pid in range(1, 5):
                    p = self.panels[pid]
                    if not p.identity_id:
                        continue
                    lines.append(f"Panel {pid}: {p.identity_name} (net: {p.network_id})")
                    # Sidebar groups/channels
                    if p.network_id:
                        groups = self.fetch_groups_direct(p.identity_id, p.network_id)
                        for g in groups:
                            lines.append(f"  [{g['group_id'][:8]}] {g['name']}")
                            chans = self.fetch_channels_direct(p.identity_id, group_id=g['group_id'])
                            for ch in chans:
                                lines.append(f"    # {ch['name']} ({ch['channel_id'][:8]})")
                    # Messages for current channel
                    if p.current_channel:
                        msgs = self.fetch_messages(p.identity_id, p.current_channel)
                        if msgs:
                            lines.append("  Messages:")
                            for m in msgs[-10:]:
                                author = m.get('author_name') or 'Unknown'
                                lines.append(f"    - {author}: {m.get('content','')}")
                output = "\n".join(lines)
            
            else:
                # Try as a message if we have an identity and channel
                panel = self.panels[self.current_cli_panel]
                if panel.identity_id and panel.current_channel and not cmd.startswith("/"):
                    result = self.send_message(self.current_cli_panel, command)
                    output = "" if result.success else f"Error: {result.error}"
                else:
                    output = f"Unknown command: {cmd}. Type 'help' for available commands."
            
            # Handle variable capture
            if capture_var and result_data:
                # Store the most relevant value from the result
                if "invite" in result_data:
                    self.variables[capture_var] = result_data["invite"]
                elif "network_id" in result_data:
                    self.variables[capture_var] = result_data["network_id"]
                else:
                    # Store first value found
                    for key, value in result_data.items():
                        if isinstance(value, str):
                            self.variables[capture_var] = value
                            break
            
            return output
                
        except (ValueError, IndexError) as e:
            return f"Command error: {e}"
        except Exception as e:
            return f"Unexpected error: {e}"
    
    def _cli_help(self) -> str:
        """Return CLI help text."""
        return """Available commands:
  /create <name>              - Create identity with name
  /network <name>             - Create a new network with default channel
  /invite                     - Generate invite code for current network
  /join <invite_code>         - Join network using invite code
  /group <name>               - Create a new group
  /channel <group_id> <name>  - Create channel in group
  groups                      - List all groups
  /panel <id>                 - Show panel state  
  /switch <id>                - Switch to a different panel (1-4)
  db                          - Show database state
  events [limit]              - Show recent events
  help                        - Show this help
  
Variable capture:
  <command> -> <var>          - Capture command output to variable
  $var or ${var}              - Use variable in commands
  
Once you have joined a channel, type anything to send a message.
Note: Use first 16 chars of group_id when creating channels."""
    
    def _format_row(self, row: Dict[str, Any]) -> str:
        """Format a database row for display."""
        formatted = {}
        for key, value in row.items():
            if isinstance(value, str) and len(value) > 40:
                formatted[key] = value[:37] + "..."
            else:
                formatted[key] = value
        return str(formatted)


# ============================================================================
# TUI Components (Only loaded if Textual is available)
# ============================================================================

if TEXTUAL_AVAILABLE:
    class HelpScreen(ModalScreen):
        """Help modal screen."""
        
        BINDINGS = [
            Binding("escape", "dismiss", "Close"),
        ]
        
        def compose(self) -> ComposeResult:
            with Container(classes="help-modal"):
                yield Static(
                    """[bold cyan]Quiet Protocol Demo - Help[/bold cyan]

[bold]Keyboard Shortcuts:[/bold]
• Tab - Switch between panels
• ? - Show this help
• q - Quit application
• Escape - Close dialogs

[bold]Panel Commands:[/bold]
• /create <name> - Create new identity
• /network <name> - Create new network
• /invite - Generate invite code
• /join <invite_code> - Join network with invite
• /group <name> - Create new group
• /channel <group_id> <name> - Create channel in group
• /channel <name> - Join existing channel

[bold]Once in a channel:[/bold]
• Type any text and press Enter to send a message

Press [bold]Escape[/bold] to close this help.""",
                    classes="help-content"
                )

    class InviteScreen(ModalScreen):
        """Full-screen modal to present an invite link with easy selection.

        Behavior:
        - ESC / Enter / Ctrl+C to dismiss
        - Click outside modal to dismiss
        - Input is focused and pre-selected for quick copy
        """

        BINDINGS = [
            Binding("escape", "dismiss", "Close"),
            Binding("enter", "dismiss", "Close"),
            Binding("ctrl+c", "dismiss", "Close"),
        ]

        def __init__(self, invite_link: str):
            super().__init__()
            self.invite_link = invite_link

        def compose(self) -> ComposeResult:
            with Container(classes="help-modal", id="invite-container"):
                yield Static("[bold cyan]Invite Link[/bold cyan]\n\nSelect and copy the link below:")
                # Use an input for easy selection/copy
                yield Input(value=self.invite_link, id="invite-input")
                yield Static("\nPress Esc to close. Tip: Hold Shift while selecting if mouse is captured.")

        def on_mount(self):
            try:
                inp = self.query_one("#invite-input", Input)
                self.set_focus(inp)
                # Select the entire link for quick copy
                inp.cursor_position = len(self.invite_link)
                inp.action_cursor_home(select=True)
            except Exception:
                pass

        def on_click(self, event) -> None:
            # Dismiss if clicked outside the invite container
            try:
                container = self.query_one("#invite-container")
                if not container.region.contains(event.x, event.y):
                    self.dismiss(True)
            except Exception:
                pass
    
    
    class ChannelsSidebar(Container):
        """Sidebar showing channels, people, and groups."""
        
        def __init__(self, core: QuietDemoCore, panel_id: int, **kwargs):
            super().__init__(**kwargs)
            self.core = core
            self.panel_id = panel_id
        
        def compose(self) -> ComposeResult:
            # Following poc-3 verbatim structure
            with VerticalScroll(classes="channel-sidebar", id=f"channels-sidebar{self.panel_id}"):
                yield Label("Channels", classes="identity-label")
                yield Container(id=f"channels-list{self.panel_id}")
                yield Label("People", classes="identity-label", id=f"people-label{self.panel_id}")
                yield Container(id=f"people-list{self.panel_id}")
                yield Label("Groups", classes="identity-label", id=f"groups-label{self.panel_id}")
                yield Container(id=f"groups-list{self.panel_id}")
    
    
    
    
    class IdentityPanel(Container):
        """Panel representing one identity/user."""
        
        def __init__(self, panel_id: int, core: QuietDemoCore, **kwargs):
            super().__init__(**kwargs)
            self.panel_id = panel_id
            self.core = core
            self.border_title = f"Panel {panel_id}"
        
        def compose(self) -> ComposeResult:
            with Container(id=f"identity-wrapper{self.panel_id}"):
                # Channel sidebar for this panel
                yield ChannelsSidebar(self.core, self.panel_id)
                
                # Panel content
                with Vertical(classes="panel-content", id=f"identity{self.panel_id}"):
                    yield Static(f"Identity {self.panel_id}: None", classes="identity-dropdown", id=f"identity{self.panel_id}-dropdown")
                    yield RichLog(classes="messages", id=f"messages{self.panel_id}", wrap=True, markup=True)
                    yield Input(placeholder="Type message or /help for commands...", id=f"input{self.panel_id}")
        
        def on_mount(self):
            """Initialize the panel on mount."""
            # Initialize the panel display
            self.update_display()
        
        
        async def _handle_command(self, command: str) -> None:
            """Handle slash commands."""
            messages_log = self.query_one(f"#messages{self.panel_id}", RichLog)
            
            parts = command[1:].split()  # Remove the /
            if not parts:
                return
            
            cmd = parts[0].lower()
            
            # Special handling for help
            if cmd == "help":
                self.app.push_screen(HelpScreen())
                return
            
            if cmd == "create" and len(parts) > 1:
                name = " ".join(parts[1:])
                # Check if this panel already has an identity
                if self.core.panels[self.panel_id].identity_id:
                    messages_log.write("[red]This panel already has an identity[/red]")
                    return
                
                try:
                    result = self.core.create_identity(self.panel_id, name)
                    if result.success:
                        messages_log.write(f"[green]{result.message}[/green]")
                        try:
                            self.app.show_snackbar(f"Identity created: {name}")
                        except Exception:
                            pass
                        # Update the display to show the new identity
                        self.update_display()
                        # Also refresh the parent app's displays
                        self.app.update_displays()
                    else:
                        messages_log.write(f"[red]Error: {result.error}[/red]")
                except Exception as e:
                    messages_log.write(f"[red]Failed to create identity: {e}[/red]")
                    print(f"Error creating identity: {e}")
            
            elif cmd == "network" and len(parts) > 1:
                network_name = " ".join(parts[1:])
                # Check if this panel has NO identity (network creation requires an identity)
                if not self.core.panels[self.panel_id].identity_id:
                    messages_log.write("[red]This panel has no identity. Create one first with /create <name>[/red]")
                    return
                
                try:
                    result = self.core.create_network(self.panel_id, network_name)
                    if result.success:
                        messages_log.write(f"[green]{result.message}[/green]")
                        try:
                            self.app.show_snackbar(f"Network created: {network_name}")
                        except Exception:
                            pass
                        # Force a state refresh before updating display
                        self.core.refresh_state(force=True)
                        # Update the display to show the network and groups
                        self.update_display()
                        # Also refresh the parent app's displays
                        self.app.update_displays()
                    else:
                        messages_log.write(f"[red]Error: {result.error}[/red]")
                except Exception as e:
                    messages_log.write(f"[red]Failed to create network: {e}[/red]")
            
            elif cmd == "invite":
                if not self.core.panels[self.panel_id].identity_id:
                    messages_log.write("[red]No identity in this panel[/red]")
                    return
                
                try:
                    result = self.core.generate_invite(self.panel_id)
                    if result.success:
                        messages_log.write(f"[green]{result.message}[/green]")
                        try:
                            self.app.show_snackbar("Invite generated")
                        except Exception:
                            pass
                        # Show invite modal for easy copy
                        invite_code = None
                        if result.data and isinstance(result.data, dict):
                            invite_code = result.data.get("invite")
                        if not invite_code and isinstance(result.message, str):
                            parts = result.message.split(": ", 1)
                            if len(parts) == 2:
                                invite_code = parts[1].strip()
                        if invite_code:
                            try:
                                self.app.push_screen(InviteScreen(invite_code))
                            except Exception:
                                pass
                        # Refresh UI so Log panel picks up the event with the link
                        try:
                            self.app.update_displays()
                        except Exception:
                            pass
                    else:
                        messages_log.write(f"[red]Error: {result.error}[/red]")
                except Exception as e:
                    messages_log.write(f"[red]Failed to generate invite: {e}[/red]")
            
            elif cmd == "join" and len(parts) > 1:
                # Support both formats:
                # /join <invite>
                # /join <name> <invite>
                invite_code = None
                chosen_name = None
                args = parts[1:]
                if len(args) == 1:
                    invite_code = args[0]
                else:
                    # Heuristic: treat the last token as the invite link
                    invite_code = args[-1]
                    chosen_name = " ".join(args[:-1]).strip() or None
                # Check if this panel already has an identity
                if self.core.panels[self.panel_id].identity_id:
                    messages_log.write("[red]This panel already has an identity. Use an empty panel to /join.[/red]")
                    try:
                        self.app.show_snackbar("Join requires an empty panel. Use Tab to switch.")
                    except Exception:
                        pass
                    try:
                        self.core._add_event('error', f"Panel {self.panel_id}: Join requires empty panel")
                    except Exception:
                        pass
                    return
                if not invite_code:
                    messages_log.write("[red]Usage: /join <invite> or /join <name> <invite>[/red]")
                    try:
                        self.app.show_snackbar("Missing invite link")
                    except Exception:
                        pass
                    return
                
                try:
                    result = self.core.join_network_with_invite(self.panel_id, invite_code, chosen_name)
                    if result.success:
                        messages_log.write(f"[green]{result.message}[/green]")
                        try:
                            self.app.show_snackbar("Joined network")
                        except Exception:
                            pass
                    else:
                        messages_log.write(f"[red]Error: {result.error}[/red]")
                        try:
                            self.app.show_snackbar("Join failed")
                        except Exception:
                            pass
                except Exception as e:
                    messages_log.write(f"[red]Failed to join network: {e}[/red]")
                    try:
                        self.app.show_snackbar("Join failed")
                    except Exception:
                        pass
            
            elif cmd == "group" and len(parts) > 1:
                group_name = " ".join(parts[1:])
                if not self.core.panels[self.panel_id].identity_id:
                    messages_log.write("[red]No identity in this panel[/red]")
                    return
                
                try:
                    result = self.core.create_group(self.panel_id, group_name)
                    if result.success:
                        messages_log.write(f"[green]{result.message}[/green]")
                        try:
                            self.app.show_snackbar(f"Group created: {group_name}")
                        except Exception:
                            pass
                    else:
                        messages_log.write(f"[red]Error: {result.error}[/red]")
                except Exception as e:
                    messages_log.write(f"[red]Failed to create group: {e}[/red]")
            
            elif cmd == "channel" and len(parts) > 1:
                # Check if it's the old format (just channel name) or new format (group_id channel_name)
                if len(parts) == 2:
                    # Old format - join existing channel
                    channel_name = parts[1]
                    result = self.core.join_channel(self.panel_id, channel_name)
                    if result.success:
                        messages_log.write(f"[green]{result.message}[/green]")
                    else:
                        messages_log.write(f"[red]Error: {result.error}[/red]")
                elif len(parts) > 2:
                    # New format - create channel in group
                    group_id = parts[1]
                    channel_name = " ".join(parts[2:])
                    if not self.core.panels[self.panel_id].identity_id:
                        messages_log.write("[red]No identity in this panel[/red]")
                        return
                    
                    try:
                        result = self.core.create_channel(self.panel_id, group_id, channel_name)
                        if result.success:
                            messages_log.write(f"[green]{result.message}[/green]")
                            # Auto-join the created channel
                            self.core.panels[self.panel_id].current_channel = channel_name
                            try:
                                self.app.show_snackbar(f"Channel created: {channel_name}")
                            except Exception:
                                pass
                        else:
                            messages_log.write(f"[red]Error: {result.error}[/red]")
                    except Exception as e:
                        messages_log.write(f"[red]Failed to create channel: {e}[/red]")
                else:
                    messages_log.write("[red]Usage: /channel <channel_name> or /channel <group_id> <channel_name>[/red]")
            
            else:
                messages_log.write(f"[red]Unknown command: /{cmd}[/red]")
        
        
        def update_display(self):
            """Update all panel components."""
            try:
                # Update identity dropdown text
                dropdown = self.query_one(f"#identity{self.panel_id}-dropdown", Static)
                panel = self.core.panels[self.panel_id]

                if panel.identity_name:
                    if panel.current_channel:
                        ch_info = self.core.get_channel_info(self.panel_id, panel.current_channel)
                        if ch_info:
                            channel_name = ch_info['name']
                            channel_text = f"#{channel_name}"
                        else:
                            channel_text = "no channel"
                    else:
                        channel_text = "no channel"
                    text = f"Identity {self.panel_id}: [bold]{panel.identity_name}[/bold] @ {channel_text}"
                else:
                    text = f"Identity {self.panel_id}: None"

                dropdown.update(text)

                # Update messages
                self._update_messages()

                # Update channel sidebar directly (no deferral)
                self._update_channels_sidebar()

            except Exception as e:
                print(f"Error updating display for panel {self.panel_id}: {e}")
        
        def _update_messages(self):
            """Update the messages display."""
            try:
                panel = self.core.panels[self.panel_id]
                messages_log = self.query_one(f"#messages{self.panel_id}", RichLog)
                # Clear the log to avoid duplicate messages
                messages_log.clear()

                if not panel.identity_id:
                    messages_log.write("[dim]Create an identity to get started. Type: /create <name>[/dim]")
                    messages_log.write("[dim]Or type /help for commands...[/dim]")
                    return

                if not panel.current_channel:
                    messages_log.write("[dim]Join or create a channel to start chatting. Use /group or /channel commands.[/dim]")
                    return

                # Fetch messages from database
                db_messages = self.core.fetch_messages(panel.identity_id, panel.current_channel)

                # Convert database messages to panel format
                from datetime import datetime
                panel.messages = []
                for msg in db_messages:
                    # Prefer author_name from query; fallback to identity lookup
                    author_name = msg.get('author_name') or "Unknown"
                    if author_name == "Unknown":
                        for ident in self.core.get_identities():
                            if ident['identity_id'] == msg.get('author_id'):
                                author_name = ident.get('name', f"User-{msg['author_id'][:8]}")
                                break

                    panel.messages.append({
                        "type": "message",
                        "from": author_name,
                        "text": msg.get('content', ''),
                        "channel": panel.current_channel,
                        "timestamp": datetime.fromtimestamp(msg.get('created_at', 0) / 1000)
                    })
                
                # Display welcome message if just joined
                ch_info = self.core.get_channel_info(self.panel_id, panel.current_channel)
                if ch_info:
                    channel_name = ch_info['name']
                    messages_log.write(f"[dim]--- #{channel_name} ---[/dim]")
                else:
                    messages_log.write(f"[dim]--- #{panel.current_channel} ---[/dim]")

                # Display messages
                if not panel.messages:
                    messages_log.write("[dim]No messages yet. Start the conversation![/dim]")
                else:
                    for msg in panel.messages[-50:]:  # Show last 50 messages
                        if msg["type"] == "system":
                            # Don't wrap system messages in additional formatting if they already have it
                            text = msg['text']
                            if not text.startswith('['):
                                messages_log.write(f"[dim italic]{text}[/dim italic]")
                            else:
                                messages_log.write(text)
                        elif msg["type"] == "message":
                            timestamp = msg['timestamp'].strftime("%H:%M")
                            from_name = msg.get('from', 'Unknown')
                            is_me = from_name == panel.identity_name
                            
                            if is_me:
                                messages_log.write(f"[dim]{timestamp}[/dim] [bold cyan]{from_name}:[/bold cyan] {msg['text']}")
                            else:
                                messages_log.write(f"[dim]{timestamp}[/dim] [green]{from_name}:[/green] {msg['text']}")
            except Exception as e:
                print(f"Error updating messages for panel {self.panel_id}: {e}")
        
        def _update_channels_sidebar(self):
            """Update the channels list."""
            try:
                # Get the channels list container
                channels_list = self.query_one(f"#channels-list{self.panel_id}", Container)

                # Clear existing content
                for child in list(channels_list.children):
                    child.remove()

                # Populate channels directly
                self._populate_channels()

            except Exception as e:
                print(f"Error updating channels sidebar for panel {self.panel_id}: {e}")
        
        def _populate_channels(self):
            """Populate the channels after clearing."""
            try:
                # If no identity, return
                panel = self.core.panels[self.panel_id]
                if not panel.identity_id:
                    return

                # Query the channels list container, but return if not found
                try:
                    channels_list = self.query_one(f"#channels-list{self.panel_id}", Container)
                except Exception:
                    # Widget might not be ready yet
                    return

                # Fetch groups and channels panel-scoped to avoid cross-panel cache issues
                groups = []
                if panel.network_id:
                    groups = self.core.fetch_groups_direct(panel.identity_id, panel.network_id)

                # Group channels by group
                widgets_to_mount = []
                # Build mapping: group_id -> group_name
                group_items = []
                if groups:
                    for g in groups:
                        group_items.append((g['group_id'], {'name': g['name']}))

                for group_id, group in sorted(group_items, key=lambda x: x[1]['name']):
                    # Group header
                    group_label = Static(f"[bold]{group['name']}[/bold]", classes="channel-group")
                    widgets_to_mount.append(group_label)

                    # Get channels in this group
                    group_channels_items = []
                    direct_channels = self.core.fetch_channels_direct(panel.identity_id, group_id=group_id)
                    if direct_channels:
                        for ch in direct_channels:
                            group_channels_items.append((ch['channel_id'], {'name': ch['name']}))
                    
                    # Add channels
                    for ch_id, channel in sorted(group_channels_items, key=lambda x: x[1]['name']):
                        btn_id = f"ch-{self.panel_id}-{ch_id}"
                        channel_btn = Button(
                            f"#{channel['name']}",
                            id=btn_id,
                            classes="channel-item"
                        )
                        
                        # Mark active channel
                        if panel.current_channel == ch_id:
                            channel_btn.add_class("--active")
                        
                        widgets_to_mount.append(channel_btn)
                
                # Mount all widgets at once
                if widgets_to_mount:
                    channels_list.mount(*widgets_to_mount)
                
                # Update people list
                self._update_people_list()
                
                # Update groups list
                self._update_groups_list()
                
            except Exception as e:
                print(f"Error populating channels for panel {self.panel_id}: {e}")
        
        def _update_people_list(self):
            """Update the people list."""
            try:
                people_list = self.query_one(f"#people-list{self.panel_id}", Container)
                
                # Clear existing content
                for child in list(people_list.children):
                    child.remove()
                
                # Build people strictly from this panel's network users
                panel = self.core.panels[self.panel_id]
                widgets = []
                users: list[dict[str, Any]] = []
                if panel.identity_id and panel.network_id:
                    try:
                        users = self.core.fetch_users(panel.identity_id, panel.network_id)
                    except Exception:
                        users = []

                for user in users:
                    name = user.get('username') or user.get('name') or f"User-{(user.get('user_id','') or user.get('peer_id',''))[:8]}"
                    person_label = Static(name, classes="person-item")
                    widgets.append(person_label)
                
                if widgets:
                    people_list.mount(*widgets)
                    
            except Exception as e:
                print(f"Error updating people list for panel {self.panel_id}: {e}")
        
        def _update_groups_list(self):
            """Update the groups list."""
            try:
                groups_list = self.query_one(f"#groups-list{self.panel_id}", Container)

                # Clear existing content
                for child in list(groups_list.children):
                    child.remove()

                # Get panel-specific groups
                panel = self.core.panels[self.panel_id]
                if not panel.identity_id or not panel.network_id:
                    return

                groups = self.core.fetch_groups_direct(panel.identity_id, panel.network_id)

                widgets = []
                for group in sorted(groups, key=lambda x: x['name']):
                    group_label = Static(f"[bold]{group['name']}[/bold]", classes="group-item")
                    widgets.append(group_label)

                    # TODO: Show members when group membership is implemented

                if widgets:
                    groups_list.mount(*widgets)
                    
            except Exception as e:
                print(f"Error updating groups list for panel {self.panel_id}: {e}")
        
        @on(Button.Pressed, ".channel-item")
        def handle_channel_click(self, event: Button.Pressed):
            """Handle channel button clicks."""
            # Extract channel ID from button ID
            parts = event.button.id.split("-", 2)  # Split into max 3 parts: ch, panel_id, channel_id
            if len(parts) == 3 and parts[0] == "ch":
                channel_id = parts[2]
                self.core.join_channel(self.panel_id, channel_id)
                # Update display
                self.update_display()
    
    
    class StateInspector(Container):
        """Database state inspector."""
        
        def __init__(self, core: QuietDemoCore, **kwargs):
            super().__init__(**kwargs)
            self.core = core
            self.border_title = "State Inspector"
        
        def compose(self) -> ComposeResult:
            yield RichLog(id="state-log", highlight=True, markup=True)
        
        def update_display(self):
            """Update the state display."""
            log = self.query_one("#state-log", RichLog)
            log.clear()
            
            # Real DB dump via API
            try:
                dump = self.core.api.dump_database(limit_per_table=200)
            except Exception as e:
                log.write(f"[red]Failed to dump DB: {e}[/red]")
                return

            def count(table: str) -> int:
                rows = dump.get(table, [])
                return len(rows) if isinstance(rows, list) else 0

            # Summary
            log.write("[bold cyan]Database State:[/bold cyan]")
            log.write(f"  peer_locals: {count('peer_locals')}")
            log.write(f"  peers: {count('peers')}")
            log.write(f"  users: {count('users')}")
            log.write(f"  groups: {count('groups')}")
            log.write(f"  channels: {count('channels')}")
            log.write(f"  messages: {count('messages')}")
            log.write(f"  events: {count('events')}")

            # Full table previews as JSON (bytes -> hex for safety)
            import json as _json

            def _to_jsonable(obj: Any):
                if isinstance(obj, (bytes, bytearray)):
                    # Render as hex string
                    try:
                        return obj.hex()
                    except Exception:
                        return str(obj)
                if isinstance(obj, dict):
                    return {k: _to_jsonable(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_to_jsonable(v) for v in obj]
                return obj
            for table, rows in dump.items():
                log.write(f"\n[bold cyan]{table}[/bold cyan] ({len(rows)} rows)")
                if not rows:
                    continue
                # Show up to first 20 rows for readability
                for i, row in enumerate(rows[:20]):
                    log.write(_json.dumps(_to_jsonable(row), sort_keys=True))
    
    
    class LogPanel(Container):
        """Log panel for success/error/info messages."""
        
        def __init__(self, core: QuietDemoCore, **kwargs):
            super().__init__(**kwargs)
            self.core = core
            self.border_title = "Log"
            self.last_event_id = 0
        
        def compose(self) -> ComposeResult:
            # Use TextLog for selectable text when available; fallback to RichLog
            if 'HAVE_TEXTLOG' in globals() and HAVE_TEXTLOG:
                yield TextLog(id="event-log", highlight=False, markup=False, wrap=False)
            else:
                yield RichLog(id="event-log", highlight=True, markup=True, auto_scroll=True)
        
        def update_display(self):
            """Update the log from in-memory core events (success/errors/info)."""
            log = self.query_one("#event-log")
            # Append only new events for readability
            for ev in self.core.events:
                if ev['id'] <= self.last_event_id:
                    continue
                ts = ev.get('timestamp', '')
                msg = ev.get('message', '')
                # Plain text for easy selection/copy
                try:
                    log.write(f"{ts} - {msg}")
                except Exception:
                    # If widget doesn't support plain write with strings, try RichLog style
                    try:
                        log.write(f"[dim]{ts}[/dim] {msg}")
                    except Exception:
                        pass
                self.last_event_id = ev['id']
        
        def _write_event(self, log: RichLog, event: Dict[str, Any]):
            # Not used anymore; retained for potential future formatting
            pass
    
    
    class QuietDemoApp(App):
        """Quiet Protocol Demo TUI Application."""
        
        CSS = """
        Screen {
            layout: grid;
            grid-size: 3 2;
            grid-gutter: 1;
        }
        
        /* Identity panels with integrated sidebars */
        IdentityPanel {
            border: solid $primary;
        }
        
        #identity-wrapper1, #identity-wrapper2, #identity-wrapper3, #identity-wrapper4 {
            layout: grid;
            grid-size: 2;
            grid-columns: 15% 1fr;
            height: 100%;
        }
        
        /* Channel sidebar */
        .channel-sidebar {
            border-right: solid $surface-lighten-1;
            padding: 0 1;
            background: $surface-lighten-1;
            overflow-y: scroll;
        }
        
        /* Labels */
        .identity-label {
            margin: 1 0;
            text-style: bold;
            color: $text-muted;
        }
        
        /* Channel items */
        .channel-item {
            width: 100%;
            text-align: left;
            margin: 0;
            padding: 0 1;
            background: transparent;
        }
        
        .channel-item:hover {
            background: $boost;
        }
        
        .channel-item.--active {
            background: $primary;
            color: $text;
        }
        
        .channel-group {
            margin: 1 0;
            text-style: bold;
            color: $text-muted;
        }
        
        /* People/Groups items */
        .person-item, .group-item {
            padding: 0 1;
            margin: 0;
        }
        
        .member-item {
            padding: 0 2;
            margin: 0;
            color: $text-muted;
        }
        
        /* Panel content */
        .panel-content {
            layout: vertical;
            padding: 0 1;
        }
        
        .identity-dropdown {
            height: 3;
            padding: 1;
            background: $boost;
            margin-bottom: 1;
        }
        
        .messages {
            height: 1fr;
            border: solid $surface-lighten-1;
            padding: 1;
        }
        
        Input {
            dock: bottom;
            margin-top: 1;
        }
        
        /* State inspector */
        StateInspector {
            border: solid magenta;
            padding: 1;
        }
        
        /* Log panel */
        LogPanel {
            border: solid green;
            padding: 1;
        }
        
        /* Help modal */
        .help-modal {
            align: center middle;
            background: $surface 80%;
        }
        
        .help-content {
            background: $panel;
            border: solid $primary;
            padding: 2 4;
            max-width: 60;
            max-height: 80%;
        }
        """
        
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("?", "help", "Help"),
            Binding("tab", "focus_next", "Next Panel", show=False),
        ]
        
        def __init__(self, core: QuietDemoCore):
            super().__init__()
            self.core = core
            self.refresh_task = None
        
        def compose(self) -> ComposeResult:
            # 3x2 grid layout:
            # Panel 1 | Panel 2 | State Inspector
            # Panel 3 | Panel 4 | Log
            
            yield IdentityPanel(1, self.core, id="panel-1")
            yield IdentityPanel(2, self.core, id="panel-2")
            yield StateInspector(self.core)
            yield IdentityPanel(3, self.core, id="panel-3") 
            yield IdentityPanel(4, self.core, id="panel-4")
            yield LogPanel(self.core)
            # Snackbar for brief success messages
            yield Static("", id="snackbar")
            
            yield Footer()
        
        async def on_input_submitted(self, event: Input.Submitted) -> None:
            """Handle input submission from any panel."""
            input_id = event.input.id
            
            if not input_id.startswith("input"):
                return
            
            # Extract panel number from input ID
            panel_num = int(input_id[-1])
            
            # Get the text and clear input
            text = event.value.strip()
            if not text:
                return
            event.input.value = ""
            
            # Find the corresponding panel
            panel = None
            for p in self.query(IdentityPanel):
                if p.panel_id == panel_num:
                    panel = p
                    break
            
            if not panel:
                return
            
            # Get the messages log for this panel
            messages_log = self.query_one(f"#messages{panel_num}", RichLog)
            
            # Process command or message
            if text.startswith("/"):
                await panel._handle_command(text)
            else:
                # Send as message
                result = panel.core.send_message(panel_num, text)
                if result.success:
                    panel._update_messages()
                else:
                    messages_log.write(f"[red]Error: {result.error}[/red]")
            
            # Update all displays
            self.update_displays()
        
        def on_mount(self):
            """Set up the app when mounted."""
            # Start periodic refresh
            self.refresh_task = self.set_interval(2, self.refresh_all)
            
            # Initial display
            self.refresh_all()
        
        def update_displays(self):
            """Update all UI displays."""
            # Refresh state from API first
            self.core.refresh_state(force=True)
            
            # Update all components
            self.refresh_all()
        
        def refresh_all(self):
            """Refresh all views."""
            self.core.refresh_state()
            
            # Update all panels
            for panel_id in range(1, 5):
                self.refresh_panel(panel_id)
            
            # Update state inspector
            inspector = self.query_one(StateInspector)
            inspector.update_display()
            
            # Update log panel
            log_panel = self.query_one(LogPanel)
            log_panel.update_display()
        
        def refresh_panel(self, panel_id: int):
            """Refresh a specific panel."""
            try:
                # Find all IdentityPanel widgets and update the one with matching panel_id
                for widget in self.query(IdentityPanel):
                    if widget.panel_id == panel_id:
                        widget.update_display()
                        break
            except Exception as e:
                print(f"Error refreshing panel {panel_id}: {e}")
        
        def action_quit(self):
            """Quit the application."""
            if self.refresh_task:
                self.refresh_task.stop()
            self.exit()

        # ---------------------------
        # Snackbar notifications
        # ---------------------------
        def show_snackbar(self, text: str, duration: float = 2.0) -> None:
            try:
                sb = self.query_one("#snackbar", Static)
                sb.update(f"[black on yellow3] {text} ")
                # Clear after duration
                self.set_timer(duration, lambda: sb.update(""))
            except Exception:
                pass
        
        def action_help(self):
            """Show help screen."""
            self.push_screen(HelpScreen())


# ============================================================================
# CLI Mode
# ============================================================================

def run_cli_mode(core: QuietDemoCore, commands: List[str] = None):
    """Run in CLI mode."""
    if commands:
        # Execute provided commands
        for cmd in commands:
            print(f"> {cmd}")
            output = core.execute_cli_command(cmd)
            if output:
                print(output)
            print()

        # After executing commands, print a snapshot similar to TUI panels
        print("=== Snapshot: Panels ===")
        for pid in range(1, 5):
            panel = core.get_panel_state(pid)
            if not panel.identity_id:
                continue
            # Refresh latest state
            core.refresh_state(force=True)
            name = panel.identity_name or panel.identity_id[:8]
            print(f"Panel {pid}: {name}")
            print(f"  Identity ID: {panel.identity_id}")
            print(f"  Network ID: {panel.network_id}")
            if panel.current_channel:
                ch_info = core.get_channel_info(pid, panel.current_channel)
                ch_name = ch_info['name'] if ch_info else panel.current_channel
                print(f"  Channel: #{ch_name} ({panel.current_channel})")
                # Show last 10 messages via API
                msgs = core.fetch_messages(panel.identity_id, panel.current_channel)
                if msgs:
                    print("  Messages:")
                    for m in msgs[-10:]:
                        author = m.get('author_name') or 'Unknown'
                        print(f"    - {author}: {m.get('content','')}")
            print()

        # Event log (raw events)
        print("=== Snapshot: Raw Events (most recent first) ===")
        try:
            dump = core.api.dump_database(limit_per_table=50)
            for ev in dump.get('events', []):
                print(ev)
        except Exception as e:
            print(f"Failed to load events: {e}")

        # DB dump overview
        print("=== Snapshot: DB Tables (counts) ===")
        try:
            dump = core.api.dump_database(limit_per_table=5)
            for table, rows in dump.items():
                print(f"{table}: {len(rows)} rows")
        except Exception as e:
            print(f"Failed to dump DB: {e}")
    else:
        # Interactive mode
        print("Quiet Protocol Demo - CLI Mode")
        print("Type 'help' for available commands")
        print()
        
        while True:
            try:
                cmd = input("> ").strip()
                if cmd.lower() in ['exit', 'quit']:
                    break
                if cmd:
                    output = core.execute_cli_command(cmd)
                    if output:
                        print(output)
                    print()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting...")
                break


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Quiet Protocol Demo")
    parser.add_argument("--cli", action="store_true",
                        help="Run in CLI mode")
    parser.add_argument("--commands", nargs="+",
                        help="Commands to run in CLI mode")
    parser.add_argument("--reset-db", action="store_true", default=True,
                        help="Reset database before starting (default: True)")
    parser.add_argument("--no-tui", action="store_true",
                        help="Disable TUI even if available")
    
    args = parser.parse_args()
    
    # Determine if we're running in CLI mode
    is_cli_mode = args.cli or args.commands or not TEXTUAL_AVAILABLE or args.no_tui
    
    try:
        # Always start from a clean demo DB between runs
        try:
            demo_db = Path(__file__).parent.parent / 'demo.db'
            if demo_db.exists():
                demo_db.unlink()
        except Exception:
            pass

        # Create core business logic with direct API access
        core = QuietDemoCore(reset_db=True)
        
        # Run in appropriate mode
        if is_cli_mode:
            # CLI mode - no terminal control changes needed
            run_cli_mode(core, args.commands)
        else:
            # TUI mode - terminal controls will be managed by Textual
            app = QuietDemoApp(core)
            app.run()
    
    finally:
        # Reset terminal controls only if we were in TUI mode
        if not is_cli_mode:
            # Comprehensive terminal reset
            # Disable mouse tracking modes
            print("\033[?1000l", end="")  # Disable X11 mouse tracking
            print("\033[?1003l", end="")  # Disable all motion tracking  
            print("\033[?1015l", end="")  # Disable urxvt mouse mode
            print("\033[?1006l", end="")  # Disable SGR mouse mode
            # Reset scroll wheel behavior
            print("\033[?1007l", end="")  # Disable alternate scroll mode
            # Exit alternate screen buffer
            print("\033[?1049l", end="")  # Exit alternate screen
            # Reset cursor
            print("\033[?25h", end="")    # Show cursor
            # Reset colors and attributes
            print("\033[0m", end="")      # Reset all attributes
            # Ensure changes take effect
            sys.stdout.flush()


if __name__ == "__main__":
    main()
