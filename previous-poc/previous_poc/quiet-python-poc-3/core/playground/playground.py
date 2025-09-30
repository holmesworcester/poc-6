#!/usr/bin/env python3
"""
Playground - A general-purpose TUI/CLI framework for protocol interaction
"""

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import yaml

from rich.console import Console
from rich.table import Table
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Static, RichLog
from textual.reactive import reactive
from textual import events

import subprocess
import time
import os


@dataclass
class WindowState:
    """State for a single window"""
    id: str
    title: str
    position: Tuple[int, int]
    command_history: List[str] = field(default_factory=list)
    history_index: int = -1
    default_command: Optional[str] = None
    output_buffer: List[Any] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)
    variables: Dict[str, str] = field(default_factory=dict)
    auto_refresh: Optional[int] = None
    map_template: Optional[str] = None
    filter_pattern: Optional[str] = None
    reduce_template: Optional[str] = None


class WindowWidget(Static):
    """A window widget with command input and output display"""
    
    def __init__(self, state: WindowState, api_client: 'APIClient', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = state
        self.api_client = api_client
        self.input = Input(placeholder=f"Enter command or press / for help")
        self.output = RichLog(highlight=True, markup=True)
        
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[bold]{self.state.title}[/bold]", classes="window-title")
            yield self.output
            yield self.input
            
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle command submission"""
        command = event.value.strip()
        if not command:
            return
            
        # Add to history
        self.state.command_history.append(command)
        self.state.history_index = -1
        
        # Clear input
        self.input.value = ""
        
        # Process command
        await self.execute_command(command)
        
    async def execute_command(self, command: str):
        """Execute a command in this window"""
        # Variable substitution
        for var, value in self.state.variables.items():
            command = command.replace(f"{{{var}}}", value)
            
        # Check for slash commands
        if command.startswith("/"):
            await self.handle_slash_command(command)
        else:
            
            parts = command.split(None, 1)
            if parts and parts[0] in self.state.aliases:
                alias_cmd = self.state.aliases[parts[0]]
                if len(parts) > 1:
                    args = parts[1].split()
                    for i, arg in enumerate(args):
                        alias_cmd = alias_cmd.replace(f"{{{i+1}}}", arg)
                    alias_cmd = alias_cmd.replace("{*}", parts[1])
                await self.execute_command(alias_cmd)
                return

            
            if self.state.default_command:
                actual_command = self.state.default_command.replace("{input}", command)
                await self.execute_command(actual_command)
                return

            
            self.add_output(f"Unknown command: {command}")
                    
    async def handle_slash_command(self, command: str):
        """Handle slash commands"""
        parts = command[1:].split(None, 1)
        if not parts:
            return
            
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        
        if cmd == "help":
            self.show_help()
        elif cmd in self.state.aliases:
            alias_cmd = self.state.aliases[cmd]
            if args:
                arg_list = args.split()
                for i, arg in enumerate(arg_list):
                    alias_cmd = alias_cmd.replace(f"{{{i+1}}}", arg)
                alias_cmd = alias_cmd.replace("{*}", args)
            await self.execute_command(alias_cmd)
        
        elif cmd == "clear":
            self.output.clear()
            self.state.output_buffer.clear()
        elif cmd == "history":
            for cmd in self.state.command_history[-10:]:
                self.add_output(cmd)
        elif cmd == "default":
            self.state.default_command = args if args else None
            self.add_output(f"Default command set to: {args}" if args else "Default command cleared")
        elif cmd == "alias":
            if args:
                alias_parts = args.split(None, 1)
                if len(alias_parts) == 2:
                    self.state.aliases[alias_parts[0]] = alias_parts[1]
                    self.add_output(f"Alias created: {alias_parts[0]} -> {alias_parts[1]}")
            else:
                for name, cmd in self.state.aliases.items():
                    self.add_output(f"{name}: {cmd}")
        elif cmd == "define":
            if args:
                var_parts = args.split(None, 1)
                if len(var_parts) == 2:
                    self.state.variables[var_parts[0]] = var_parts[1]
                    self.add_output(f"Variable defined: {var_parts[0]} = {var_parts[1]}")
            else:
                for name, value in self.state.variables.items():
                    self.add_output(f"{name}: {value}")
        elif cmd == "api":
            await self.handle_api_command(args)
        elif cmd == "echo":
            self.add_output(args)
        else:
            self.add_output(f"Unknown command: /{cmd}")
            
    async def handle_api_command(self, args: str):
        """Handle API commands"""
        parts = args.split(None, 2)
        if len(parts) < 2:
            self.add_output("Usage: /api <METHOD> <path> [data]")
            return
            
        method = parts[0].upper()
        path = parts[1]
        data = parts[2] if len(parts) > 2 else None
        
        try:
            result = await self.api_client.request(method, path, data)
            self.add_output(json.dumps(result, indent=2))
        except Exception as e:
            self.add_output(f"API Error: {str(e)}")
            
    def show_help(self):
        """Show help for this window"""
        help_text = """[bold]Window Commands:[/bold]
  /help              - Show this help
  /clear             - Clear window output
  /history           - Show command history
  /default <cmd>     - Set default command for plain input
  /alias <name> <cmd> - Create command alias
  /define <var> <val> - Define a variable
  /api <METHOD> <path> [data] - Make API call
  
[bold]Variable substitution:[/bold] {varname}
[bold]Alias arguments:[/bold] {1}, {2}, {*} (all args)
"""
        self.add_output(help_text)
        
        # Show existing aliases if any
        if self.state.aliases:
            self.add_output("\n[bold]Current Aliases:[/bold]")
            for name, cmd in self.state.aliases.items():
                self.add_output(f"  {name}: {cmd}")
        
        # Show existing variables if any
        if self.state.variables:
            self.add_output("\n[bold]Current Variables:[/bold]")
            for name, value in self.state.variables.items():
                self.add_output(f"  {name} = {value}")
        
    def add_output(self, text: str):
        """Add text to output buffer and display"""
        self.state.output_buffer.append(text)
        # RichLog automatically handles scrolling
        self.output.write(text)


class APIClient:
    """API client using the api.py CLI tool"""
    
    def __init__(self, protocol_path: str):
        self.protocol_path = protocol_path
        
    async def request(self, method: str, path: str, data: Optional[str] = None) -> Any:
        """Make an API request using api.py CLI"""
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        protocol_arg = os.path.basename(self.protocol_path) if ('/' in self.protocol_path or '\\' in self.protocol_path) else self.protocol_path
        cmd = [sys.executable, "-m", "core.api", protocol_arg, method, path]
        
        # Parse data if provided
        if data:
            json_data = None
            try:
                json_data = json.loads(data)
            except json.JSONDecodeError:
                # Try to parse as key=value pairs
                json_data = {}
                for pair in data.split():
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        json_data[key] = value
            
            if json_data:
                cmd.extend(["--data", json.dumps(json_data)])
                
        # Run the command from project root
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_root)
        
        if result.returncode != 0:
            raise Exception(f"API error: {result.stderr}")
            
        # Parse the output
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            # Return as-is if not JSON
            return result.stdout
            
    async def close(self):
        pass


class PlaygroundApp(App):
    """Main TUI application"""
    
    CSS = """
    .window-container {
        border: solid white;
        height: 100%;
    }
    
    .window-title {
        text-align: center;
        background: $primary;
        padding: 1;
    }
    
    .output {
        height: 1fr;
        overflow-y: scroll;
        padding: 1;
    }
    
    Input {
        dock: bottom;
    }
    """
    
    def __init__(self, config: Dict[str, Any], protocol_path: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config
        self.protocol_path = protocol_path
        self.windows: Dict[str, WindowWidget] = {}
        self.api_client = APIClient(protocol_path)
        
    def compose(self) -> ComposeResult:
        """Compose the app layout"""
        yield Header()
        
        # Parse layout
        layout = self.config.get("layout", "2x2")
        if isinstance(layout, str):
            rows, cols = map(int, layout.split("x"))
        else:
            rows, cols = layout
            
        # Create window states from config
        window_configs = self.config.get("windows", [])
        window_states = []
        
        for i, window_cfg in enumerate(window_configs):
            state = WindowState(
                id=window_cfg.get("id", f"window_{i}"),
                title=window_cfg.get("title", f"Window {i+1}"),
                position=tuple(window_cfg.get("position", [i // cols, i % cols])),
                default_command=window_cfg.get("default_command"),
                aliases=window_cfg.get("aliases", {})
            )
            window_states.append(state)
            
        # Create grid layout
        with Container():
            for row in range(rows):
                with Horizontal():
                    for col in range(cols):
                        # Find window for this position
                        window_state = None
                        for state in window_states:
                            if state.position == (row, col):
                                window_state = state
                                break
                                
                        if not window_state:
                            # Create default window
                            window_state = WindowState(
                                id=f"window_{row}_{col}",
                                title=f"Window {row},{col}",
                                position=(row, col)
                            )
                            
                        window = WindowWidget(window_state, self.api_client, classes="window-container")
                        self.windows[window_state.id] = window
                        yield window
                        
        yield Footer()
        
    async def on_mount(self) -> None:
        """App mounted - ready to use"""
        pass
        
    async def action_quit(self) -> None:
        """Quit the app"""
        await self.api_client.close()
        await super().action_quit()


def load_config(config_path: Optional[str]) -> Dict[str, Any]:
    """Load configuration from YAML file"""
    if not config_path:
        # Default configuration
        return {
            "layout": "2x2",
            "windows": [
                {
                    "id": "main",
                    "title": "Main",
                    "position": [0, 0]
                }
            ]
        }
        
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


class CLIExecutor:
    """CLI mode executor"""
    
    def __init__(self, protocol_path: str, config: Dict[str, Any]):
        self.protocol_path = protocol_path
        self.config = config
        self.variables = {}
        self.aliases = {}

        # Merge aliases from all windows so CLI mode has access to them
        windows = config.get("windows", [])
        for w in windows:
            for k, v in (w.get("aliases", {}) or {}).items():
                # Do not allow alias to override an existing alias already set earlier
                if k not in self.aliases:
                    self.aliases[k] = v
            
    async def execute_command(self, command: str) -> str:
        """Execute a single command and return result"""
        # Variable substitution
        for var, value in self.variables.items():
            command = command.replace(f"{{{var}}}", value)
            
        # Handle slash commands
        if command.startswith("/"):
            return await self.handle_slash_command(command)
            
        # Check aliases
        parts = command.split(None, 1)
        if parts and parts[0] in self.aliases:
            alias_cmd = self.aliases[parts[0]]
            if len(parts) > 1:
                # Substitute arguments
                args = parts[1].split()
                for i, arg in enumerate(args):
                    alias_cmd = alias_cmd.replace(f"{{{i+1}}}", arg)
                alias_cmd = alias_cmd.replace("{*}", parts[1])
            return await self.execute_command(alias_cmd)
            
        return f"Unknown command: {command}"
        
    async def handle_slash_command(self, command: str) -> str:
        """Handle slash commands in CLI mode"""
        parts = command[1:].split(None, 1)
        if not parts:
            return "Empty command"
            
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        
        if cmd == "define":
            if args:
                var_parts = args.split(None, 1)
                if len(var_parts) == 2:
                    self.variables[var_parts[0]] = var_parts[1]
                    return f"Variable defined: {var_parts[0]} = {var_parts[1]}"
            else:
                return "\n".join(f"{k}: {v}" for k, v in self.variables.items())
                
        elif cmd == "alias":
            if args:
                alias_parts = args.split(None, 1)
                if len(alias_parts) == 2:
                    self.aliases[alias_parts[0]] = alias_parts[1]
                    return f"Alias created: {alias_parts[0]} -> {alias_parts[1]}"
            else:
                return "\n".join(f"{k}: {v}" for k, v in self.aliases.items())
                
        elif cmd == "api":
            return await self.handle_api_command(args)
        
        elif cmd == "echo":
            return args
        elif cmd in self.aliases:
            alias_cmd = self.aliases[cmd]
            if args:
                arg_list = args.split()
                for i, arg in enumerate(arg_list):
                    alias_cmd = alias_cmd.replace(f"{{{i+1}}}", arg)
                alias_cmd = alias_cmd.replace("{*}", args)
            return await self.execute_command(alias_cmd)

        else:
            return f"Unknown command: /{cmd}"
            
    async def handle_api_command(self, args: str) -> str:
        """Handle API commands"""
        parts = args.split(None, 2)
        if len(parts) < 2:
            return "Usage: /api <METHOD> <path> [data]"
            
        method = parts[0].upper()
        path = parts[1]
        data = parts[2] if len(parts) > 2 else None
        
        # Build command - run from project root
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        protocol_arg = os.path.basename(self.protocol_path) if ('/' in self.protocol_path or '\\' in self.protocol_path) else self.protocol_path
        cmd = [sys.executable, "-m", "core.api", protocol_arg, method, path]
        
        # Parse and add data if provided
        if data:
            json_data = None
            try:
                json_data = json.loads(data)
            except json.JSONDecodeError:
                # Try key=value format
                json_data = {}
                for pair in data.split():
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        json_data[key] = value
            
            if json_data:
                cmd.extend(["--data", json.dumps(json_data)])
                
        # Run the command from project root
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_root)
        
        if result.returncode != 0:
            return f"API Error: {result.stderr}"
            
        return result.stdout
            
    async def run_commands(self, commands: List[str]):
        """Run a list of commands"""
        for command in commands:
            command = command.strip()
            if command and not command.startswith('#'):
                print(f"> {command}")
                result = await self.execute_command(command)
                print(result)
                print()


class SyncCLIExecutor:
    """Synchronous version of CLIExecutor for environments where asyncio is not available."""
    def __init__(self, protocol_path: str, config: Dict[str, Any]):
        self.protocol_path = protocol_path
        self.config = config
        self.variables = {}
        self.aliases = {}
        windows = config.get("windows", [])
        for w in windows:
            for k, v in (w.get("aliases", {}) or {}).items():
                if k not in self.aliases:
                    self.aliases[k] = v

    def execute_command(self, command: str) -> str:
        for var, value in self.variables.items():
            command = command.replace(f"{{{var}}}", value)

        if command.startswith("/"):
            return self.handle_slash_command(command)

        parts = command.split(None, 1)
        if parts and parts[0] in self.aliases:
            alias_cmd = self.aliases[parts[0]]
            if len(parts) > 1:
                args = parts[1].split()
                for i, arg in enumerate(args):
                    alias_cmd = alias_cmd.replace(f"{{{i+1}}}", arg)
                alias_cmd = alias_cmd.replace("{*}", parts[1])
            return self.execute_command(alias_cmd)

        return f"Unknown command: {command}"

    def handle_slash_command(self, command: str) -> str:
        parts = command[1:].split(None, 1)
        if not parts:
            return "Empty command"

        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "define":
            if args:
                var_parts = args.split(None, 1)
                if len(var_parts) == 2:
                    self.variables[var_parts[0]] = var_parts[1]
                    return f"Variable defined: {var_parts[0]} = {var_parts[1]}"
            else:
                return "\n".join(f"{k}: {v}" for k, v in self.variables.items())

        if cmd == "alias":
            if args:
                alias_parts = args.split(None, 1)
                if len(alias_parts) == 2:
                    self.aliases[alias_parts[0]] = alias_parts[1]
                    return f"Alias created: {alias_parts[0]} -> {alias_parts[1]}"
            else:
                return "\n".join(f"{k}: {v}" for k, v in self.aliases.items())

        if cmd == "api":
            return self.handle_api_command(args)

        if cmd == "echo":
            return args

        if cmd in self.aliases:
            alias_cmd = self.aliases[cmd]
            if args:
                arg_list = args.split()
                for i, arg in enumerate(arg_list):
                    alias_cmd = alias_cmd.replace(f"{{{i+1}}}", arg)
                alias_cmd = alias_cmd.replace("{*}", args)
            return self.execute_command(alias_cmd)

        return f"Unknown command: /{cmd}"

    def handle_api_command(self, args: str) -> str:
        parts = args.split(None, 2)
        if len(parts) < 2:
            return "Usage: /api <METHOD> <path> [data]"

        method = parts[0].upper()
        path = parts[1]
        data = parts[2] if len(parts) > 2 else None

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        protocol_arg = os.path.basename(self.protocol_path) if ('/' in self.protocol_path or '\\' in self.protocol_path) else self.protocol_path
        cmd = [sys.executable, "-m", "core.api", protocol_arg, method, path]

        if data:
            json_data = None
            try:
                json_data = json.loads(data)
            except json.JSONDecodeError:
                json_data = {}
                for pair in data.split():
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        json_data[key] = value
            if json_data:
                cmd.extend(["--data", json.dumps(json_data)])

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_root)
        if result.returncode != 0:
            return f"API Error: {result.stderr}"
        return result.stdout

    def run_commands(self, commands: List[str]):
        for command in commands:
            command = command.strip()
            if command and not command.startswith('#'):
                print(f"> {command}")
                result = self.execute_command(command)
                print(result)
                print()
                
def run_cli_mode(args, config):
    """Run playground in CLI mode

    Attempt to run with the async CLIExecutor first. If the environment
    prevents asyncio from creating an event loop (PermissionError/OSError),
    fall back to a synchronous executor (SyncCLIExecutor) which executes
    /api commands via subprocesses. This keeps the protocol-agnostic logic
    entirely in YAML and the executor implementations.
    """
    # Primary (async) executor
    executor = CLIExecutor(args.protocol, config)

    # Helper to run commands synchronously via SyncCLIExecutor
    def run_sync_commands():
        sync_executor = SyncCLIExecutor(args.protocol, config)
        if args.cli:
            sync_executor.run_commands([args.cli])
            return True
        if args.cli_file:
            with open(args.cli_file, 'r') as f:
                commands = f.readlines()
            sync_executor.run_commands(commands)
            return True
        if args.cli_interactive:
            print("Playground CLI (type 'exit' to quit)")
            while True:
                try:
                    command = input("> ")
                    if command.lower() in ['exit', 'quit']:
                        break
                    result = sync_executor.execute_command(command)
                    print(result)
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    print(f"Error: {e}")
            return True
        return False

    # If the environment disallows socketpair (asyncio internals), skip async entirely
    try:
        import socket
        s1, s2 = socket.socketpair()
        s1.close(); s2.close()
        can_use_async = True
    except Exception:
        can_use_async = False

    if can_use_async:
        # Try async executor
        try:
            if args.cli:
                asyncio.run(executor.run_commands([args.cli]))
                return
            if args.cli_file:
                with open(args.cli_file, 'r') as f:
                    commands = f.readlines()
                asyncio.run(executor.run_commands(commands))
                return
            if args.cli_interactive:
                async def interactive():
                    print("Playground CLI (type 'exit' to quit)")
                    while True:
                        try:
                            command = input("> ")
                            if command.lower() in ['exit', 'quit']:
                                break
                            result = await executor.execute_command(command)
                            print(result)
                        except KeyboardInterrupt:
                            break
                        except Exception as e:
                            print(f"Error: {e}")

                asyncio.run(interactive())
                return
        except (PermissionError, OSError):
            # Fall through to sync fallback
            pass

    # Fallback to synchronous execution
    ran = run_sync_commands()
    if ran:
        return


def main():
    parser = argparse.ArgumentParser(description="Playground - Protocol interaction framework")
    parser.add_argument("protocol", help="Protocol path (e.g., protocols/message_via_tor)")
    parser.add_argument("--config", help="Configuration file path")
    parser.add_argument("--cli", help="Execute single command and exit")
    parser.add_argument("--cli-file", help="Execute commands from file")
    parser.add_argument("--cli-interactive", action="store_true", help="Interactive CLI mode")
    
    args = parser.parse_args()

    # Determine if the positional argument is a config YAML file or a protocol path
    config_path = None
    protocol = args.protocol

    if os.path.exists(protocol) and protocol.lower().endswith(('.yaml', '.yml')):
        # The user passed a YAML config as the first positional arg: use it
        config_path = protocol
        cfg = load_config(config_path)
        protocol = cfg.get('protocol')
        if not protocol:
            parser.error(f"Config {config_path} does not specify a 'protocol' field")
        # override args so downstream code can use args.protocol and args.config
        args.protocol = protocol
        args.config = config_path
        config = cfg
    else:
        # Not a YAML config file; use provided --config if any
        config = load_config(args.config)

    if args.cli or args.cli_file or args.cli_interactive:
        # CLI mode
        run_cli_mode(args, config)
    else:
        # TUI mode
        app = PlaygroundApp(config, args.protocol)
        app.run()


if __name__ == "__main__":
    main()