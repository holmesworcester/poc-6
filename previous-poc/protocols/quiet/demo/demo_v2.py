#!/usr/bin/env python3
"""
Quiet Protocol Demo v2 - Refactored with command-returns-state pattern

This demo provides both CLI and TUI interfaces for testing the Quiet protocol.
Every command returns the complete relevant state, eliminating timing issues.
"""

import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent.parent.parent))

from core.api import APIClient, APIError
from protocols.quiet import client as qapi

# ============================================================================
# Command Result with State
# ============================================================================

@dataclass
class CommandResult:
    """Commands return both status and current state"""
    success: bool
    message: str
    error: Optional[str] = None

    # State snapshot after command execution
    identity_name: Optional[str] = None
    identity_id: Optional[str] = None
    peer_id: Optional[str] = None
    network_id: Optional[str] = None
    network_name: Optional[str] = None
    current_channel_id: Optional[str] = None
    current_channel_name: Optional[str] = None

    # Collections returned by command
    groups: List[Dict[str, Any]] = field(default_factory=list)
    channels: List[Dict[str, Any]] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    users: List[Dict[str, Any]] = field(default_factory=list)

    def format_output(self) -> str:
        """Format result for display"""
        lines = []

        # Status message
        if self.success:
            lines.append(f"✓ {self.message}")
        else:
            lines.append(f"✗ {self.error or self.message}")

        # Show state if relevant
        if self.identity_name:
            lines.append(f"Identity: {self.identity_name}")

        if self.network_name:
            lines.append(f"Network: {self.network_name}")

        if self.current_channel_name:
            lines.append(f"Channel: #{self.current_channel_name}")

        # Show collections if returned
        if self.groups:
            lines.append(f"Groups ({len(self.groups)}):")
            for g in self.groups:
                lines.append(f"  • {g['name']} [{g['group_id'][:8]}]")
                # Show channels in this group
                group_channels = [c for c in self.channels if c.get('group_id') == g['group_id']]
                for c in group_channels:
                    lines.append(f"    # {c['name']} [{c['channel_id'][:8]}]")

        if self.users:
            lines.append(f"Users ({len(self.users)}):")
            for u in self.users[:5]:  # Show first 5
                name = u.get('username') or u.get('name') or 'Unknown'
                lines.append(f"  • {name}")

        if self.messages:
            lines.append(f"Recent messages:")
            for m in self.messages[-5:]:  # Show last 5
                sender = m.get('sender_name', 'Unknown')
                content = m.get('content', '')
                lines.append(f"  [{sender}]: {content}")

        return '\n'.join(lines)


# ============================================================================
# Panel State
# ============================================================================

@dataclass
class PanelState:
    """Complete state for a panel"""
    panel_id: int

    # Identity
    identity_name: Optional[str] = None
    identity_id: Optional[str] = None
    peer_id: Optional[str] = None

    # Network
    network_id: Optional[str] = None
    network_name: Optional[str] = None

    # Current context
    current_channel_id: Optional[str] = None
    current_group_id: Optional[str] = None

    # UI state
    input_buffer: str = ""


# ============================================================================
# Unified Demo Core
# ============================================================================

class UnifiedDemoCore:
    """Core that returns full state with each command"""

    def __init__(self, protocol_dir: Path = None, reset_db: bool = False):
        if protocol_dir is None:
            protocol_dir = Path(__file__).parent.parent

        self.protocol_dir = protocol_dir
        self.api = APIClient(protocol_dir=protocol_dir, reset_db=reset_db)

        # Initialize panels (4 panels: 3 visual + 1 CLI)
        self.panels: Dict[int, PanelState] = {
            i: PanelState(panel_id=i) for i in range(1, 5)
        }

    def execute_command(self, panel_id: int, command: str) -> CommandResult:
        """Execute command and return resulting state"""
        try:
            # Handle empty command
            if not command or not command.strip():
                return CommandResult(False, "", error="Empty command")

            # Parse command
            parts = command.strip().split()
            cmd = parts[0].lstrip('/')
            args = parts[1:] if len(parts) > 1 else []

            # Route to command handler
            if cmd == "create" and args:
                return self._create_identity(panel_id, args[0])
            elif cmd == "network" and args:
                return self._create_network(panel_id, ' '.join(args))
            elif cmd == "invite":
                return self._generate_invite(panel_id)
            elif cmd == "join" and args:
                # Support both orders: /join <invite> <name> OR /join <name> <invite>
                # Detect which is the invite code (starts with quiet://)
                if len(args) >= 2:
                    if args[0].startswith('quiet://'):
                        invite_code = args[0]
                        name = args[1]
                    else:
                        name = args[0]
                        invite_code = args[1]
                elif len(args) == 1:
                    if args[0].startswith('quiet://'):
                        invite_code = args[0]
                        name = None
                    else:
                        return CommandResult(False, "", error="Invalid join command. Use: /join <invite_code> <name> or /join <name> <invite_code>")
                else:
                    return CommandResult(False, "", error="Invalid join command. Use: /join <invite_code> <name>")
                return self._join_network(panel_id, invite_code, name)
            elif cmd == "channel":
                return self._handle_channel_command(panel_id, args)
            elif cmd == "group" and args:
                return self._create_group(panel_id, ' '.join(args))
            elif cmd == "refresh":
                return self._refresh_state(panel_id)
            elif cmd == "dbstate":
                return self._show_db_state(panel_id)
            elif cmd == "help":
                return self._show_help()
            elif cmd == "switch" and args:
                try:
                    target = int(args[0])
                    return CommandResult(True, f"Switched to panel {target}")
                except ValueError:
                    return CommandResult(False, "", error="Invalid panel number")
            elif not cmd.startswith('/'):
                # No slash = send as message
                return self._send_message(panel_id, command)
            else:
                return CommandResult(False, "", error=f"Unknown command: /{cmd}")

        except Exception as e:
            # Catch any unhandled exceptions at the top level
            return CommandResult(False, "", error=f"Command failed: {str(e)}")

    def _create_identity(self, panel_id: int, name: str) -> CommandResult:
        """Create identity and return state"""
        try:
            panel = self.panels[panel_id]

            if panel.identity_id:
                return CommandResult(False, "", error="Panel already has an identity")

            # Create identity
            identity_result = qapi.core_identity_create(self.api, {'name': name})
            identity_id = identity_result['ids']['identity']

            # Create peer
            peer_result = qapi.create_peer(self.api, {
                'identity_id': identity_id,
                'username': name
            })
            peer_id = peer_result['ids']['peer']

            # Update panel state
            panel.identity_id = identity_id
            panel.identity_name = name
            panel.peer_id = peer_id

            return CommandResult(
                success=True,
                message=f"Created identity: {name}",
                identity_name=name,
                identity_id=identity_id,
                peer_id=peer_id
            )

        except APIError as e:
            return CommandResult(False, "", error=str(e))
        except Exception as e:
            return CommandResult(False, "", error=f"Failed to create identity: {str(e)}")

    def _create_network(self, panel_id: int, name: str) -> CommandResult:
        """Create network and return all groups/channels"""
        panel = self.panels[panel_id]

        if not panel.peer_id:
            return CommandResult(False, "", error="Create an identity first")

        try:
            # Create network
            network_result = qapi.create_network(self.api, {
                'name': name,
                'peer_id': panel.peer_id
            })
            network_id = network_result['ids']['network']

            # Join network as user
            qapi.create_user(self.api, {
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
            group_id = group_result['ids']['group']

            # Create default channel
            channel_result = qapi.create_channel(self.api, {
                'name': 'general',
                'group_id': group_id,
                'peer_id': panel.peer_id,
                'network_id': network_id
            })
            channel_id = channel_result['ids']['channel']

            # Update panel state
            panel.network_id = network_id
            panel.network_name = name
            panel.current_channel_id = channel_id
            panel.current_group_id = group_id

            # Fetch ALL current state
            groups = qapi.group_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': network_id
            })

            channels = qapi.channel_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': network_id
            })

            users = qapi.user_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': network_id
            })

            return CommandResult(
                success=True,
                message=f"Created network: {name}",
                identity_name=panel.identity_name,
                identity_id=panel.identity_id,
                peer_id=panel.peer_id,
                network_id=network_id,
                network_name=name,
                current_channel_id=channel_id,
                current_channel_name='general',
                groups=groups,
                channels=channels,
                users=users
            )

        except APIError as e:
            return CommandResult(False, "", error=str(e))

    def _generate_invite(self, panel_id: int) -> CommandResult:
        """Generate invite and return state"""
        panel = self.panels[panel_id]

        if not panel.network_id:
            return CommandResult(False, "", error="No network selected")

        if not panel.peer_id:
            return CommandResult(False, "", error="No peer identity")

        try:
            # Get first group if no current group
            if not panel.current_group_id:
                groups = qapi.group_get(self.api, {
                    'identity_id': panel.identity_id,
                    'network_id': panel.network_id
                })
                if groups:
                    panel.current_group_id = groups[0]['group_id']

            # Create invite
            result = qapi.create_invite(self.api, {
                'network_id': panel.network_id,
                'peer_id': panel.peer_id,
                'group_id': panel.current_group_id or ''
            })

            invite_link = result.get('data', {}).get('invite_link', 'No invite link returned')

            return CommandResult(
                success=True,
                message=f"Invite code: {invite_link}",
                identity_name=panel.identity_name,
                network_id=panel.network_id,
                network_name=panel.network_name
            )

        except APIError as e:
            return CommandResult(False, "", error=str(e))

    def _join_network(self, panel_id: int, invite_code: str, name: Optional[str] = None) -> CommandResult:
        """Join network with invite code"""
        panel = self.panels[panel_id]

        try:
            # join_as_user always creates a new identity, so we only support that path
            if panel.identity_id:
                return CommandResult(False, "", error="Panel already has an identity. Use a different panel to join.")

            if not name:
                return CommandResult(False, "", error="Name required: /join <invite_code> <name>")

            # Join as user - creates identity, peer, and user
            result = qapi.join_as_user(self.api, {
                'invite_link': invite_code,  # API expects 'invite_link' not 'invite_code'
                'name': name
            })

            # Extract IDs from result
            identity_id = result['ids']['identity']
            peer_id = result['ids']['peer']
            network_id = result['ids'].get('network')

            # Update panel state
            panel.identity_id = identity_id
            panel.identity_name = name
            panel.peer_id = peer_id
            panel.network_id = network_id

            # Fetch network data
            groups = []
            channels = []
            users = []

            if network_id:
                groups = qapi.group_get(self.api, {
                    'identity_id': identity_id,
                    'network_id': network_id
                })

                channels = qapi.channel_get(self.api, {
                    'identity_id': identity_id,
                    'network_id': network_id
                })

                users = qapi.user_get(self.api, {
                    'identity_id': identity_id,
                    'network_id': network_id
                })

                # Set first channel as current
                if channels:
                    panel.current_channel_id = channels[0]['channel_id']

            return CommandResult(
                success=True,
                message=f"Joined network as: {name}",
                identity_name=name,
                identity_id=identity_id,
                peer_id=peer_id,
                network_id=network_id,
                groups=groups,
                channels=channels,
                users=users
            )

        except ValueError as e:
            # Handle invalid invite format errors
            return CommandResult(False, "", error=f"Invalid invite code: {str(e)}")
        except APIError as e:
            return CommandResult(False, "", error=str(e))
        except Exception as e:
            # Catch any other unexpected errors
            return CommandResult(False, "", error=f"Failed to join network: {str(e)}")

    def _handle_channel_command(self, panel_id: int, args: List[str]) -> CommandResult:
        """Handle channel commands - create or join"""
        panel = self.panels[panel_id]

        if not panel.identity_id:
            return CommandResult(False, "", error="No identity selected")

        if not args:
            return CommandResult(False, "", error="Usage: /channel <name> or /channel <name> in <group-name>")

        # Parse "channel name in group-name" format
        if 'in' in args:
            in_idx = args.index('in')
            channel_name = ' '.join(args[:in_idx])
            group_name = ' '.join(args[in_idx + 1:])

            # Find group by name
            groups = qapi.group_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': panel.network_id
            })

            group_id = None
            for g in groups:
                if g['name'].lower() == group_name.lower():
                    group_id = g['group_id']
                    break

            if not group_id:
                return CommandResult(False, "", error=f"Group '{group_name}' not found")

            # Create channel in group
            try:
                result = qapi.create_channel(self.api, {
                    'name': channel_name,
                    'group_id': group_id,
                    'peer_id': panel.peer_id,
                    'network_id': panel.network_id
                })

                channel_id = result['ids']['channel']
                panel.current_channel_id = channel_id

                # Fetch updated channels
                channels = qapi.channel_get(self.api, {
                    'identity_id': panel.identity_id,
                    'network_id': panel.network_id
                })

                return CommandResult(
                    success=True,
                    message=f"Created channel: {channel_name} in {group_name}",
                    current_channel_id=channel_id,
                    current_channel_name=channel_name,
                    groups=groups,
                    channels=channels
                )

            except APIError as e:
                return CommandResult(False, "", error=str(e))

        else:
            # Join existing channel by name
            channel_name = ' '.join(args)

            if not panel.network_id:
                return CommandResult(False, "", error="No network selected")

            channels = qapi.channel_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': panel.network_id
            })

            for c in channels:
                if c['name'].lower() == channel_name.lower():
                    panel.current_channel_id = c['channel_id']
                    return CommandResult(
                        success=True,
                        message=f"Joined channel: {channel_name}",
                        current_channel_id=c['channel_id'],
                        current_channel_name=channel_name,
                        channels=channels
                    )

            return CommandResult(False, "", error=f"Channel '{channel_name}' not found")

    def _create_group(self, panel_id: int, name: str) -> CommandResult:
        """Create group and return state"""
        panel = self.panels[panel_id]

        if not panel.network_id:
            return CommandResult(False, "", error="No network selected")

        if not panel.peer_id:
            return CommandResult(False, "", error="No peer identity")

        try:
            # Create group
            result = qapi.create_group(self.api, {
                'name': name,
                'network_id': panel.network_id,
                'peer_id': panel.peer_id
            })

            group_id = result['ids']['group']

            # Create default channel in the group
            channel_result = qapi.create_channel(self.api, {
                'name': 'general',
                'group_id': group_id,
                'peer_id': panel.peer_id,
                'network_id': panel.network_id
            })

            # Fetch updated state
            groups = qapi.group_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': panel.network_id
            })

            channels = qapi.channel_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': panel.network_id
            })

            return CommandResult(
                success=True,
                message=f"Created group: {name} with #general channel",
                groups=groups,
                channels=channels
            )

        except APIError as e:
            return CommandResult(False, "", error=str(e))

    def _send_message(self, panel_id: int, content: str) -> CommandResult:
        """Send message and return updated message list"""
        panel = self.panels[panel_id]

        if not panel.current_channel_id:
            return CommandResult(False, "", error="No channel selected")

        if not panel.peer_id:
            return CommandResult(False, "", error="No peer identity")

        try:
            # Send message
            msg_result = qapi.create_message(self.api, {
                'content': content,
                'channel_id': panel.current_channel_id,
                'peer_id': panel.peer_id
            })

            # Fetch ALL messages for the channel
            messages = qapi.message_get(self.api, {
                'identity_id': panel.identity_id,
                'channel_id': panel.current_channel_id
            })

            # Add sender names
            for msg in messages:
                msg['sender_name'] = panel.identity_name

            return CommandResult(
                success=True,
                message="Message sent",
                messages=messages
            )

        except APIError as e:
            return CommandResult(False, "", error=str(e))

    def _show_db_state(self, panel_id: int) -> CommandResult:
        """Show database state"""
        lines = []
        lines.append("\n═══ Database State ═══\n")

        # Show all panels and their identities
        lines.append("Panels:")
        for pid, panel in self.panels.items():
            if panel.identity_id:
                lines.append(f"  Panel {pid}: {panel.identity_name or 'unnamed'} ({panel.identity_id[:8]}...)")
                if panel.network_name:
                    lines.append(f"    Network: {panel.network_name} ({panel.network_id[:8] if panel.network_id else ''}...)")
                if panel.current_channel_id:
                    lines.append(f"    Channel ID: {panel.current_channel_id[:8]}...")
            else:
                lines.append(f"  Panel {pid}: (no identity)")

        # Show all networks
        lines.append("\nNetworks:")
        networks_shown = set()
        for pid, panel in self.panels.items():
            if panel.network_id and panel.network_id not in networks_shown:
                networks_shown.add(panel.network_id)
                lines.append(f"  {panel.network_name or 'unnamed'} ({panel.network_id[:8]}...)")

                # Get groups/channels for this network
                if panel.identity_id:
                    try:
                        groups = self.api.query('group_get', {
                            'identity_id': panel.identity_id,
                            'network_id': panel.network_id
                        })
                        for group in groups:
                            lines.append(f"    └─ {group['name']} (group)")

                            channels = self.api.query('channel_get', {
                                'identity_id': panel.identity_id,
                                'group_id': group['group_id']
                            })
                            for channel in channels:
                                lines.append(f"        └─ #{channel['name']}")
                    except:
                        pass

        # Show recent events from event store
        lines.append("\nRecent Events:")
        try:
            # Use sqlite3 directly to query event store
            import sqlite3
            db = sqlite3.connect(str(self.api.db_path))
            cursor = db.execute(
                "SELECT event_type, event_id FROM events "
                "WHERE event_type IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 10"
            )
            events = cursor.fetchall()
            db.close()

            if events:
                for event_type, event_id in events:
                    lines.append(f"  {event_type}: {event_id[:8]}...")
            else:
                lines.append("  (no events)")
        except Exception as e:
            lines.append(f"  Error reading events: {e}")

        # Show message counts
        lines.append("\nMessage Counts:")
        for pid, panel in self.panels.items():
            if panel.current_channel_id:
                try:
                    messages = self.api.query('message_get', {
                        'identity_id': panel.identity_id,
                        'channel_id': panel.current_channel_id
                    })
                    count = len(messages) if messages else 0
                    lines.append(f"  Panel {pid}: {count} messages in current channel")
                except:
                    pass

        return CommandResult(True, "\n".join(lines))

    def _refresh_state(self, panel_id: int) -> CommandResult:
        """Refresh and return current state"""
        panel = self.panels[panel_id]

        if not panel.identity_id:
            return CommandResult(
                success=True,
                message="No identity in panel",
                identity_name=panel.identity_name
            )

        groups = []
        channels = []
        users = []
        messages = []

        if panel.network_id:
            groups = qapi.group_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': panel.network_id
            })

            channels = qapi.channel_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': panel.network_id
            })

            users = qapi.user_get(self.api, {
                'identity_id': panel.identity_id,
                'network_id': panel.network_id
            })

        if panel.current_channel_id:
            messages = qapi.message_get(self.api, {
                'identity_id': panel.identity_id,
                'channel_id': panel.current_channel_id
            })

        # Find current channel name
        current_channel_name = None
        for c in channels:
            if c['channel_id'] == panel.current_channel_id:
                current_channel_name = c['name']
                break

        return CommandResult(
            success=True,
            message="State refreshed",
            identity_name=panel.identity_name,
            identity_id=panel.identity_id,
            peer_id=panel.peer_id,
            network_id=panel.network_id,
            network_name=panel.network_name,
            current_channel_id=panel.current_channel_id,
            current_channel_name=current_channel_name,
            groups=groups,
            channels=channels,
            users=users,
            messages=messages
        )

    def _show_help(self) -> CommandResult:
        """Show help text"""
        help_text = """Commands:
  /create <name>         - Create new identity
  /network <name>        - Create new network
  /invite                - Generate invite code
  /join <invite> <name>  - Join network with invite
  /join <name> <invite>  - Join network (alt order)
  /group <name>          - Create new group
  /channel <name>        - Join existing channel
  /channel <name> in <group> - Create channel in group
  /refresh               - Refresh current state
  /switch <panel>        - Switch to panel (1-4)
  /dbstate               - Show database state
  /help                  - Show this help

  Any text without / sends a message to current channel"""

        return CommandResult(
            success=True,
            message=help_text
        )


# ============================================================================
# CLI Interface
# ============================================================================

def run_cli(args):
    """Run in CLI mode"""

    # Initialize core
    core = UnifiedDemoCore(reset_db=args.reset_db)
    current_panel = 1

    print("Quiet Protocol Demo v2 - CLI Mode")
    print("Type /help for commands, 'quit' to exit\n")

    # Process initial commands if provided
    if args.commands:
        for cmd in args.commands:
            print(f"Panel {current_panel}> {cmd}")

            # Handle panel switches
            if cmd.startswith('/switch'):
                try:
                    current_panel = int(cmd.split()[1])
                except (IndexError, ValueError):
                    pass

            result = core.execute_command(current_panel, cmd)
            print(result.format_output())
            print()

    # Interactive mode
    if not args.commands or args.interactive:
        while True:
            try:
                # Show prompt with current state
                panel = core.panels[current_panel]
                prompt_parts = [f"Panel {current_panel}"]

                if panel.identity_name:
                    prompt_parts.append(panel.identity_name)

                if panel.current_channel_id:
                    # Get channel name
                    result = core._refresh_state(current_panel)
                    if result.current_channel_name:
                        prompt_parts.append(f"#{result.current_channel_name}")

                prompt = f"[{' @ '.join(prompt_parts)}]> "

                # Get input
                cmd = input(prompt)

                if cmd.lower() in ['quit', 'exit', 'q']:
                    break

                # Handle panel switches
                if cmd.startswith('/switch'):
                    try:
                        current_panel = int(cmd.split()[1])
                        print(f"Switched to panel {current_panel}")
                        continue
                    except (IndexError, ValueError):
                        print("Usage: /switch <panel-number>")
                        continue

                # Execute command
                result = core.execute_command(current_panel, cmd)
                print(result.format_output())

            except KeyboardInterrupt:
                print("\nUse 'quit' to exit")
            except EOFError:
                break

    print("\nGoodbye!")


# ============================================================================
# TUI Interface with Embedded CLI
# ============================================================================

try:
    from textual.app import App, ComposeResult
    from textual.containers import Container, Horizontal, Vertical
    from textual.widgets import Static, Input, Button, RichLog, Label, TextArea
    from textual.binding import Binding
    from textual import events

    TUI_AVAILABLE = True

    class CLIPanel(Container):
        """Interactive CLI panel with selectable text output"""

        def __init__(self, core: UnifiedDemoCore, panel_id: int):
            super().__init__()
            self.core = core
            self.panel_id = panel_id
            self.command_history = []
            self.history_index = 0

        def compose(self) -> ComposeResult:
            yield Label(f"Panel {self.panel_id} - Interactive CLI", classes="panel-header")
            yield RichLog(
                id=f"cli-output-{self.panel_id}",
                highlight=True,
                markup=True,
                auto_scroll=True,
                wrap=True,
                classes="cli-output"
            )
            yield Input(
                placeholder="Enter command or /help",
                id=f"cli-input-{self.panel_id}",
                classes="cli-input"
            )

        def on_input_submitted(self, event: Input.Submitted):
            """Handle command input"""
            if event.input.id != f"cli-input-{self.panel_id}":
                return

            command = event.value
            if not command:
                return

            output_log = self.query_one(f"#cli-output-{self.panel_id}", RichLog)

            # Add to history
            self.command_history.append(command)
            self.history_index = len(self.command_history)

            # Show command
            panel = self.core.panels[self.panel_id]
            prompt = f"[{panel.identity_name or 'no-identity'}]> "
            output_log.write(f"{prompt}{command}")

            # Execute command
            result = self.core.execute_command(self.panel_id, command)

            # Show result
            output_log.write(result.format_output())
            output_log.write("")  # Empty line

            # Clear input
            event.input.clear()

            # Update other visual panels if they exist
            self.app.update_visual_panels(result)

    class VisualPanel(Container):
        """Visual panel that updates from command results"""

        def __init__(self, core: UnifiedDemoCore, panel_id: int):
            super().__init__()
            self.core = core
            self.panel_id = panel_id
            self.last_state = CommandResult(True, "")

        def compose(self) -> ComposeResult:
            # Header
            yield Static(
                f"Panel {self.panel_id}: No identity",
                id=f"header-{self.panel_id}",
                classes="panel-header"
            )
            # Main content area
            with Horizontal(classes="panel-content"):
                # Sidebar
                with Vertical(classes="sidebar"):
                    yield Label("Groups & Channels", classes="sidebar-header")
                    yield Container(id=f"sidebar-{self.panel_id}", classes="sidebar-content")
                    yield Label("Users", classes="sidebar-header")
                    yield Container(id=f"users-{self.panel_id}", classes="sidebar-content")

                # Messages area
                with Vertical(classes="messages-area"):
                    # Use TextArea for selectable messages
                    yield TextArea(
                        id=f"messages-{self.panel_id}",
                        read_only=True,
                        classes="messages"
                    )
                    yield Input(
                        placeholder="Type message or /command",
                        id=f"input-{self.panel_id}",
                        classes="message-input"
                    )

        def on_input_submitted(self, event: Input.Submitted):
            """Handle input as command or message"""
            if not event.input.id.startswith(f"input-{self.panel_id}"):
                return

            text = event.value
            if not text:
                return

            # Show what was typed in the messages area
            messages_area = self.query_one(f"#messages-{self.panel_id}", TextArea)
            current_text = messages_area.text
            new_text = f"> {text}\n"
            messages_area.load_text(current_text + new_text)
            messages_area.scroll_end()

            # Execute as command or message
            if text.startswith('/'):
                result = self.core.execute_command(self.panel_id, text)
            else:
                # Send as message
                result = self.core.execute_command(self.panel_id, text)

            # Show result message
            if result.message:
                current_text = messages_area.text
                if result.success:
                    new_text = f"✓ {result.message}\n"
                else:
                    new_text = f"✗ {result.error or result.message}\n"
                messages_area.load_text(current_text + new_text)
                messages_area.scroll_end()

            # Update display
            self.update_from_result(result)

            # Clear input
            event.input.clear()

            # Update other panels
            self.app.update_visual_panels(result)

        def update_from_result(self, result: CommandResult):
            """Update visual elements from command result"""
            panel = self.core.panels[self.panel_id]

            # Update header
            header = self.query_one(f"#header-{self.panel_id}", Static)
            header_parts = [f"Panel {self.panel_id}"]

            if panel.identity_name:
                header_parts.append(panel.identity_name)

            if result.current_channel_name:
                header_parts.append(f"#{result.current_channel_name}")
            elif panel.current_channel_id:
                header_parts.append("#unknown")

            header.update(": ".join(header_parts))

            # Update sidebar with groups and channels
            # Only update if we have new data, otherwise keep existing
            if result.groups is not None and len(result.groups) > 0:
                sidebar = self.query_one(f"#sidebar-{self.panel_id}", Container)
                sidebar.remove_children()
                for group in result.groups:
                    # Group header
                    sidebar.mount(Static(f"[bold]{group['name']}[/bold]", classes="group-header"))

                    # Channels in group
                    group_channels = [
                        c for c in result.channels
                        if c.get('group_id') == group['group_id']
                    ]

                    for channel in group_channels:
                        is_active = channel['channel_id'] == panel.current_channel_id
                        prefix = "→ " if is_active else "  "
                        sidebar.mount(
                            Static(
                                f"{prefix}#{channel['name']}",
                                classes="channel-active" if is_active else "channel"
                            )
                        )

            # Update users list
            users_container = self.query_one(f"#users-{self.panel_id}", Container)
            users_container.remove_children()

            if result.users:
                for user in result.users[:10]:  # Show first 10
                    name = user.get('username') or user.get('name') or 'Unknown'
                    users_container.mount(Static(f"• {name}", classes="user"))

            # Update messages if returned
            if result.messages:
                messages_area = self.query_one(f"#messages-{self.panel_id}", TextArea)
                # Build new text from messages
                text_lines = []
                for msg in result.messages:
                    sender = msg.get('sender_name', 'Unknown')
                    content = msg.get('content', '')
                    text_lines.append(f"{sender}: {content}")

                messages_area.load_text("\n".join(text_lines))
                messages_area.scroll_end()

            self.last_state = result

    class QuietDemoTUI(App):
        """TUI with embedded CLI"""

        CSS = """
        .panel-header {
            background: $boost;
            padding: 0 1;
            height: 1;
            dock: top;
        }

        .sidebar {
            width: 25;
            border-right: solid $primary;
        }

        .sidebar-header {
            padding: 0 1;
            background: $surface;
            text-style: bold;
        }

        .sidebar-content {
            padding: 0 1;
            height: auto;
        }

        .group-header {
            margin-top: 1;
        }

        .channel {
            padding-left: 2;
        }

        .channel-active {
            padding-left: 2;
            text-style: bold;
            color: $success;
        }

        .messages-area {
            min-width: 40;
            height: 1fr;
        }

        .messages {
            border: solid $primary;
            padding: 1;
            height: 1fr;
        }

        .messages TextArea {
            background: $surface;
        }

        .cli-output {
            border: solid $primary;
            padding: 1;
            height: 1fr;
        }

        .panel-content {
            height: 1fr;
        }

        .message-input, .cli-input {
            dock: bottom;
            height: 3;
        }

        .user {
            padding-left: 1;
        }

        #left-panels {
            width: 50%;
            height: 100%;
        }

        #right-panels {
            width: 50%;
            height: 100%;
        }

        VisualPanel {
            height: 50%;
            min-height: 10;
            border: solid $primary;
        }

        CLIPanel {
            height: 50%;
            min-height: 10;
            border: solid $primary;
        }

        DBStatePanel {
            height: 50%;
            min-height: 10;
            border: solid $primary;
        }

        .db-output {
            border: solid $secondary;
            padding: 1;
            height: 1fr;
        }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("tab", "switch_panel", "Switch Panel"),
        ]

        def __init__(self, core: UnifiedDemoCore):
            super().__init__()
            self.core = core
            self.current_panel = 1
            # Disable mouse support through class attribute
            self.mouse_over_widget = None

        def compose(self) -> ComposeResult:
            with Horizontal():
                # Left side - two visual panels
                with Vertical(id="left-panels"):
                    yield VisualPanel(self.core, 1)
                    yield VisualPanel(self.core, 3)

                # Right side - visual panel + CLI
                with Vertical(id="right-panels"):
                    yield VisualPanel(self.core, 2)
                    yield CLIPanel(self.core, 4)

        def update_visual_panels(self, result: CommandResult):
            """Update all visual panels with new state if relevant"""
            # For now, only update the panel that executed the command
            # In future, could update all panels in same network
            pass

        def action_switch_panel(self):
            """Switch between panels"""
            self.current_panel = (self.current_panel % 4) + 1
            self.notify(f"Switched to panel {self.current_panel}")

        def action_quit(self):
            """Quit the application"""
            self.exit()

    def run_tui(args):
        """Run in TUI mode"""
        import sys
        import subprocess

        core = UnifiedDemoCore(reset_db=args.reset_db)
        app = QuietDemoTUI(core)

        try:
            app.run()
        finally:
            # Reset terminal mouse controls after exit
            if sys.platform != 'win32':
                # Disable mouse reporting sequences
                print('\033[?1000l\033[?1002l\033[?1003l\033[?1006l', end='', flush=True)
                # Reset cursor
                print('\033[?25h', end='', flush=True)
                # Try using tput for additional reset
                try:
                    subprocess.run(['tput', 'rmcup'], capture_output=True)
                except:
                    pass

except ImportError:
    TUI_AVAILABLE = False

    def run_tui(args):
        """TUI not available"""
        print("=" * 60)
        print("TUI mode requires the textual library")
        print("Install with: pip install textual")
        print("=" * 60)
        print("\nFalling back to CLI mode...\n")
        run_cli(args)


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Quiet Protocol Demo v2')
    parser.add_argument('--cli', action='store_true', help='Run in CLI mode instead of TUI')
    parser.add_argument('--reset-db', action='store_true', help='Reset database on startup')
    parser.add_argument('--commands', nargs='+', help='Commands to execute (forces CLI mode)')
    parser.add_argument('--interactive', action='store_true', help='Enter interactive mode after commands')

    args = parser.parse_args()

    # Default to TUI mode unless --cli is specified or commands are provided
    if args.cli or args.commands:
        run_cli(args)
    else:
        # TUI mode is the default
        run_tui(args)


if __name__ == '__main__':
    main()