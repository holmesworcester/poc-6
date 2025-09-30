# Demo Refactoring Plan V2: Command-Returns-State Pattern

## Core Concept

Every command returns the complete relevant state, eliminating the need for separate refresh operations. The TUI becomes a visual wrapper around the CLI, with the CLI embedded directly in the interface.

## Architecture

### 1. Enhanced Command Results
```python
@dataclass
class CommandResult:
    """Commands return both status and current state"""
    success: bool
    message: str
    error: Optional[str] = None

    # State snapshot after command execution
    state: Optional[PanelState] = None

    # Specific data for the command
    groups: Optional[List[dict]] = None
    channels: Optional[List[dict]] = None
    messages: Optional[List[dict]] = None
    users: Optional[List[dict]] = None
```

### 2. Stateless Command Execution
```python
class QuietDemoCore:
    """Core that returns full state with each command"""

    def execute_command(self, panel_id: int, command: str) -> CommandResult:
        """Execute command and return resulting state"""

        # Parse command
        parts = command.strip().split()
        if not parts:
            return CommandResult(False, error="Empty command")

        cmd = parts[0].lstrip('/')
        args = parts[1:]

        # Execute command
        if cmd == "create" and args:
            result = self._create_identity(panel_id, args[0])
        elif cmd == "network" and args:
            result = self._create_network(panel_id, ' '.join(args))
        elif cmd == "channel" and args:
            result = self._create_channel(panel_id, args)
        elif cmd == "message" and args:
            result = self._send_message(panel_id, ' '.join(args))
        elif cmd == "refresh":
            result = self._refresh_state(panel_id)
        else:
            result = CommandResult(False, error=f"Unknown command: {cmd}")

        # Always include current state in result
        if result.success:
            result.state = self._get_panel_state(panel_id)

        return result

    def _create_network(self, panel_id: int, name: str) -> CommandResult:
        """Create network and return all groups/channels"""
        panel = self.panels[panel_id]

        # Create network
        network_result = qapi.create_network(self.api, {
            'name': name,
            'peer_id': panel.peer_id
        })
        network_id = network_result['ids']['network']

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

        # Fetch and return ALL current state
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
            groups=groups,
            channels=channels,
            users=users
        )

    def _send_message(self, panel_id: int, content: str) -> CommandResult:
        """Send message and return updated message list"""
        panel = self.panels[panel_id]

        # Send message
        msg_result = qapi.create_message(self.api, {
            'content': content,
            'channel_id': panel.current_channel,
            'peer_id': panel.peer_id
        })

        # Fetch and return ALL messages for the channel
        messages = qapi.message_get(self.api, {
            'identity_id': panel.identity_id,
            'channel_id': panel.current_channel
        })

        return CommandResult(
            success=True,
            message=f"Message sent",
            messages=messages
        )
```

### 3. Unified CLI/TUI Interface
```python
class UnifiedPanel:
    """Panel that works identically in CLI and TUI mode"""

    def __init__(self, core: QuietDemoCore, panel_id: int):
        self.core = core
        self.panel_id = panel_id

        # Current display state (updated from command results)
        self.groups: List[dict] = []
        self.channels: List[dict] = []
        self.messages: List[dict] = []
        self.users: List[dict] = []

    def execute(self, command: str) -> str:
        """Execute command and update display state"""
        result = self.core.execute_command(self.panel_id, command)

        # Update our display state from the result
        if result.groups is not None:
            self.groups = result.groups
        if result.channels is not None:
            self.channels = result.channels
        if result.messages is not None:
            self.messages = result.messages
        if result.users is not None:
            self.users = result.users

        # Return formatted output for CLI/TUI display
        return self._format_result(result)

    def _format_result(self, result: CommandResult) -> str:
        """Format result for display"""
        lines = []

        # Status message
        if result.success:
            lines.append(f"✓ {result.message}")
        else:
            lines.append(f"✗ {result.error}")

        # Show updated state if provided
        if result.groups:
            lines.append(f"Groups: {len(result.groups)}")
            for g in result.groups[:3]:  # Show first 3
                lines.append(f"  • {g['name']}")

        if result.channels:
            lines.append(f"Channels: {len(result.channels)}")
            for c in result.channels[:3]:
                lines.append(f"  # {c['name']}")

        return '\n'.join(lines)

    def get_sidebar_data(self) -> dict:
        """Get current state for sidebar display"""
        return {
            'groups': self.groups,
            'channels': self.channels,
            'users': self.users
        }
```

### 4. TUI with Embedded CLI
```python
class TUIWithCLI(App):
    """TUI with embedded interactive CLI in bottom right"""

    def compose(self):
        # Left panels - visual identities
        yield Container(
            IdentityPanel(self.core, 1),
            IdentityPanel(self.core, 3),
            classes="left-panels"
        )

        # Right side
        yield Container(
            # Top right - visual identity
            IdentityPanel(self.core, 2),

            # Bottom right - Interactive CLI with selectable text
            CLIPanel(self.core, 4),

            classes="right-panels"
        )

class CLIPanel(Widget):
    """Interactive CLI embedded in TUI - fully selectable text"""

    def compose(self):
        # Use TextLog for selectable output
        yield TextLog(
            id="cli-output",
            highlight=True,
            markup=True,
            auto_scroll=True,
            wrap=True
        )
        yield Input(
            placeholder="Enter command or /help",
            id="cli-input"
        )

    def on_input_submitted(self, event: Input.Submitted):
        """Handle command input"""
        command = event.value
        output_log = self.query_one("#cli-output", TextLog)

        # Show command
        output_log.write(f"> {command}")

        # Execute and show result
        result = self.panel.execute(command)
        output_log.write(result)

        # Clear input
        event.input.clear()

        # Update other panels' displays from the state
        self.app.update_all_panels()

class IdentityPanel(Widget):
    """Visual panel that updates from command results"""

    def __init__(self, core: QuietDemoCore, panel_id: int):
        self.panel = UnifiedPanel(core, panel_id)

    def compose(self):
        # Visual elements
        yield Container(
            Static("Groups & Channels"),
            Container(id=f"sidebar-{self.panel_id}"),
            Static("Messages"),
            RichLog(id=f"messages-{self.panel_id}"),
            Input(id=f"input-{self.panel_id}")
        )

    def on_input_submitted(self, event: Input.Submitted):
        """Handle input as command or message"""
        text = event.value

        if text.startswith('/'):
            # Command - execute and update
            result = self.panel.execute(text)
            self.update_from_state()
        else:
            # Message - send and update
            result = self.panel.execute(f"/message {text}")
            self.update_from_state()

        event.input.clear()

    def update_from_state(self):
        """Update visual elements from panel state"""
        sidebar_data = self.panel.get_sidebar_data()

        # Update sidebar
        sidebar = self.query_one(f"#sidebar-{self.panel_id}")
        sidebar.clear()

        for group in sidebar_data['groups']:
            sidebar.mount(Static(f"[bold]{group['name']}[/bold]"))

            # Get channels for this group
            group_channels = [
                c for c in sidebar_data['channels']
                if c.get('group_id') == group['group_id']
            ]

            for channel in group_channels:
                sidebar.mount(Button(f"# {channel['name']}"))
```

## Key Benefits

1. **No Timing Issues**: Commands return the state immediately
2. **Unified Logic**: CLI and TUI use identical command execution
3. **Testable**: Can test commands independently of UI
4. **LLM-Friendly**: Clear command -> result pattern
5. **Interactive CLI in TUI**: Bottom right panel is a full CLI for debugging
6. **Selectable Text**: Using TextLog allows text selection in the CLI output

## Testing Strategy

```python
def test_command_returns_state():
    """Test that commands return complete state"""
    core = QuietDemoCore()

    # Create identity
    result = core.execute_command(1, "/create alice")
    assert result.success
    assert result.state.identity_name == "alice"

    # Create network - should return groups and channels
    result = core.execute_command(1, "/network test-net")
    assert result.success
    assert len(result.groups) == 1
    assert len(result.channels) == 1
    assert result.groups[0]['name'] == 'public'
    assert result.channels[0]['name'] == 'general'

    # Send message - should return message list
    result = core.execute_command(1, "Hello world")  # No slash = message
    assert result.success
    assert len(result.messages) > 0
    assert result.messages[-1]['content'] == "Hello world"

def test_cli_tui_equivalence():
    """Test that CLI and TUI produce same results"""
    core = QuietDemoCore()

    # CLI execution
    cli_panel = UnifiedPanel(core, 1)
    cli_output = cli_panel.execute("/create alice")
    cli_state = cli_panel.get_sidebar_data()

    # TUI would use same panel class
    tui_panel = UnifiedPanel(core, 2)
    tui_output = tui_panel.execute("/create bob")
    tui_state = tui_panel.get_sidebar_data()

    # Both should work identically
    assert "Created identity" in cli_output
    assert "Created identity" in tui_output
```

## Implementation Steps

1. Create `CommandResult` class with state fields
2. Update all command methods to return full state
3. Create `UnifiedPanel` that works for both CLI and TUI
4. Implement `CLIPanel` widget with TextLog for selectable output
5. Update `IdentityPanel` to use command results for updates
6. Remove all refresh_state and timer-based updates
7. Add comprehensive tests