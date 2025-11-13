#!/usr/bin/env python3
"""
POC-6 CLI - Interactive and non-interactive command-line interface.

This CLI uses ONLY event functions from events/ modules - no direct database access.
Same constraints as scenario tests - acts like an API client.
"""

import sqlite3
import sys
import argparse
from typing import Optional, Dict, List, Any
from db import Database
import schema
import tick

# Import event functions (this is our API)
from events.identity import user, peer, invite, network
from events.content import channel, message
from events.group import group_member


class AccountContext:
    """
    Represents an account in the CLI session.

    'Account' is the frontend term - internally corresponds to a peer.
    """

    def __init__(self, user_name: str, device_name: str, peer_id: str, peer_shared_id: str):
        self.user_name = user_name        # User's display name (e.g., "alice")
        self.device_name = device_name    # Device name (e.g., "desktop", "phone")
        self.peer_id = peer_id            # Backend peer ID
        self.peer_shared_id = peer_shared_id
        self.user_id: Optional[str] = None       # Set after network join
        self.network_id: Optional[str] = None    # Set after network join

    @property
    def full_name(self) -> str:
        """Full account name for display: 'alice (desktop)'"""
        return f"{self.user_name} ({self.device_name})"


class CLISession:
    """Manages the CLI session state."""

    def __init__(self):
        self.db: Optional[Database] = None
        self.accounts: Dict[str, AccountContext] = {}  # full_name -> AccountContext
        self.selected_account: Optional[str] = None    # Currently selected account full_name
        self.selected_channel_id: Optional[str] = None  # Currently selected channel ID
        self.current_time_ms: int = 0
        self.auto_tick_count: int = 100  # Number of auto-ticks after event commands (default 100)
        self.last_invite_link: Optional[str] = None  # Internal - for --join-with-last-invite
        self.last_link_url: Optional[str] = None     # Internal - for --with-last-link

    def initialize_database(self):
        """Initialize in-memory database with schema."""
        conn = sqlite3.Connection(":memory:")
        self.db = Database(conn)
        schema.create_all(self.db)

    def get_selected_account(self) -> AccountContext:
        """Get the currently selected account context."""
        if not self.selected_account:
            raise ValueError("no account selected")
        return self.accounts[self.selected_account]

    def get_account_by_number(self, n: int) -> Optional[AccountContext]:
        """Get account by number (1-indexed)."""
        account_list = list(self.accounts.values())
        if 1 <= n <= len(account_list):
            return account_list[n - 1]
        return None

    def add_account(self, account: AccountContext):
        """Add an account to the session."""
        self.accounts[account.full_name] = account

    def run_auto_tick(self):
        """Run auto-tick if enabled."""
        if self.auto_tick_count > 0:
            print(f"⟳ auto-syncing {self.auto_tick_count} ticks...")
            start_t = self.current_time_ms
            for _ in range(self.auto_tick_count):
                self.current_time_ms += 100  # 100ms per tick
                tick.tick(t_ms=self.current_time_ms, db=self.db)
            print(f"✓ synced (t={start_t}ms -> {self.current_time_ms}ms)")
            print()


# ============================================================================
# STATE DISPLAY FUNCTIONS (using event module query functions only)
# ============================================================================

def display_state(session: CLISession):
    """Display the complete three-section state."""
    display_accounts(session)
    print()
    display_sidebar(session)
    print()
    display_main(session)


def display_accounts(session: CLISession):
    """Display numbered ACCOUNTS section."""
    print("ACCOUNTS:")
    if not session.accounts:
        print("  (no accounts)")
        return

    account_list = list(session.accounts.values())
    for i, account in enumerate(account_list, 1):
        selected = "*" if account.full_name == session.selected_account else " "
        short_user = account.user_id[:6] if account.user_id else "???"
        short_peer = account.peer_id[:6] if account.peer_id else "???"
        short_net = account.network_id[:6] if account.network_id else "???"
        print(f"  {i}. {selected} {account.full_name} - user_{short_user}, peer_{short_peer}, network_{short_net}")


def display_sidebar(session: CLISession):
    """Display SIDEBAR section with users and channels."""
    if not session.selected_account:
        print("SIDEBAR:")
        print("  (no account selected)")
        return

    account = session.get_selected_account()
    print(f"SIDEBAR ({account.full_name}):")

    # Users section (informational only)
    print("  users:")
    if account.network_id:
        # Get all_users group ID
        all_users_group_id = network.get_all_users_group_id(account.network_id, account.peer_id, session.db)
        # List members
        members = group_member.list_members(all_users_group_id, account.peer_id, session.db)
        if members:
            for i, member in enumerate(members, 1):
                # Map user_id to username
                user_id = member.get('user_id', '???')
                username = '???'
                for acc in session.accounts.values():
                    if acc.user_id == user_id:
                        username = acc.user_name
                        break
                print(f"    {i}. {username}")
        else:
            print("    (no users)")
    else:
        print("    (no users)")

    print()

    # Channels section (selectable)
    print("  channels:")
    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)
    if channels:
        for i, ch in enumerate(channels, 1):
            selected = "*" if ch['channel_id'] == session.selected_channel_id else " "
            print(f"    {i}. {selected} #{ch['name']}")
    else:
        print("    (no channels)")


def display_main(session: CLISession):
    """Display MAIN section with messages in selected channel."""
    if not session.selected_account:
        print("MAIN:")
        print("  (no account selected)")
        return

    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("MAIN:")
        print("  (no channel selected)")
        return

    # Get channel name
    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)
    channel_name = None
    for ch in channels:
        if ch['channel_id'] == session.selected_channel_id:
            channel_name = ch['name']
            break

    if not channel_name:
        print("MAIN:")
        print("  (channel not found)")
        return

    print(f"MAIN (#{channel_name}):")

    # Get messages
    messages = message.list_messages(session.selected_channel_id, account.peer_id, session.db)
    if not messages:
        print("  (no messages)")
        return

    for msg in messages:
        # Map author_id (peer_shared_id) to username
        author_peer_shared_id = msg.get('author_id', '???')
        author_name = '???'
        for acc in session.accounts.values():
            if acc.peer_shared_id == author_peer_shared_id:
                author_name = acc.user_name
                break
        timestamp = msg.get('created_at', 0)
        content = msg.get('content', '')
        print(f"  [{timestamp}ms] {author_name}: {content}")


# ============================================================================
# COMMANDS
# ============================================================================

def cmd_new_network(session: CLISession, name: str, device: str):
    """Create a new network and first account."""
    result = user.new_network(name=name, t_ms=session.current_time_ms, db=session.db)

    # Create account context
    account = AccountContext(
        user_name=name.lower(),
        device_name=device.lower(),
        peer_id=result['peer_id'],
        peer_shared_id=result['peer_shared_id']
    )
    account.user_id = result['user_id']
    account.network_id = result['network_id']

    session.add_account(account)
    session.selected_account = account.full_name
    session.selected_channel_id = result['channel_id']  # #general

    session.db.commit()
    session.current_time_ms += 100

    account_num = list(session.accounts.keys()).index(account.full_name) + 1

    print(f"✓ created network as {name.lower()}")
    print(f"✓ selected account #{account_num}: {account.full_name}")
    print(f"✓ selected channel #1: #general")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_switch(session: CLISession, account_num: int):
    """Switch to a different account."""
    account = session.get_account_by_number(account_num)
    if not account:
        print(f"✗ account #{account_num} not found")
        return

    session.selected_account = account.full_name

    # Auto-select first channel if available
    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)
    if channels:
        session.selected_channel_id = channels[0]['channel_id']

    print(f"✓ selected account #{account_num}: {account.full_name}")
    print()

    display_state(session)


def cmd_send(session: CLISession, msg: str):
    """Send a message to the currently selected channel."""
    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("✗ no channel selected")
        return

    result = message.create(
        peer_id=account.peer_id,
        channel_id=session.selected_channel_id,
        content=msg,
        t_ms=session.current_time_ms,
        db=session.db
    )

    session.db.commit()
    session.current_time_ms += 100

    print("✓ sent message")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_sync(session: CLISession, ticks: int):
    """Run manual sync ticks."""
    print(f"⟳ syncing {ticks} ticks...")
    start_t = session.current_time_ms
    for _ in range(ticks):
        session.current_time_ms += 100
        tick.tick(t_ms=session.current_time_ms, db=session.db)
    print(f"✓ synced (t={start_t}ms -> {session.current_time_ms}ms)")
    print()

    display_state(session)


def cmd_set_auto_tick(session: CLISession, count: int):
    """Set auto-tick count."""
    session.auto_tick_count = count
    if count == 0:
        print("✓ auto-tick disabled")
    else:
        print(f"✓ auto-tick set to {count} ticks")


def cmd_select_channel(session: CLISession, channel_num: int):
    """Select a channel by number."""
    account = session.get_selected_account()

    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)
    if not channels:
        print("✗ no channels available")
        return

    if not (1 <= channel_num <= len(channels)):
        print(f"✗ channel #{channel_num} not found (must be 1-{len(channels)})")
        return

    selected_channel = channels[channel_num - 1]
    session.selected_channel_id = selected_channel['channel_id']

    print(f"✓ selected channel #{channel_num}: #{selected_channel['name']}")
    print()

    display_state(session)


def cmd_create_channel(session: CLISession, name: str):
    """Create a new channel."""
    account = session.get_selected_account()

    if not account.network_id:
        print("✗ no network joined")
        return

    # Get all_users group for the channel
    all_users_group_id = network.get_all_users_group_id(account.network_id, account.peer_id, session.db)

    result = channel.create(
        name=name,
        peer_id=account.peer_id,
        peer_shared_id=account.peer_shared_id,
        t_ms=session.current_time_ms,
        db=session.db,
        group_id=all_users_group_id
    )

    session.db.commit()
    session.current_time_ms += 100

    print(f"✓ created channel #{name}")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_quit(session: CLISession):
    """Quit the CLI."""
    print("goodbye!")
    sys.exit(0)


# ============================================================================
# INTERACTIVE MODE
# ============================================================================

def run_interactive(session: CLISession):
    """Run interactive REPL mode."""
    print("welcome to poc-6 cli (interactive mode)")
    print("type 'help' for commands, 'quit' to exit")
    print()

    display_state(session)
    print()

    while True:
        try:
            line = input("> ").strip()
            if not line:
                continue

            parts = line.split()
            cmd = parts[0]

            if cmd == "quit" or cmd == "exit":
                cmd_quit(session)

            elif cmd == "new-network":
                parser = argparse.ArgumentParser(add_help=False)
                parser.add_argument("--name", required=True)
                parser.add_argument("--device", required=True)
                try:
                    args = parser.parse_args(parts[1:])
                    cmd_new_network(session, args.name, args.device)
                except SystemExit:
                    print("usage: new-network --name <name> --device <device>")

            elif cmd == "switch":
                if len(parts) < 2:
                    print("usage: switch <n>")
                else:
                    try:
                        cmd_switch(session, int(parts[1]))
                    except ValueError:
                        print("error: account number must be an integer")

            elif cmd == "send":
                if len(parts) < 2:
                    print("usage: send <message>")
                else:
                    msg = " ".join(parts[1:]).strip('"')
                    cmd_send(session, msg)

            elif cmd == "sync":
                parser = argparse.ArgumentParser(add_help=False)
                parser.add_argument("--ticks", type=int, required=True)
                try:
                    args = parser.parse_args(parts[1:])
                    cmd_sync(session, args.ticks)
                except SystemExit:
                    print("usage: sync --ticks <n>")

            elif cmd == "set-auto-tick":
                if len(parts) < 2:
                    print("usage: set-auto-tick <n>")
                else:
                    try:
                        cmd_set_auto_tick(session, int(parts[1]))
                    except ValueError:
                        print("error: count must be an integer")

            elif cmd == "select-channel":
                if len(parts) < 2:
                    print("usage: select-channel <n>")
                else:
                    try:
                        cmd_select_channel(session, int(parts[1]))
                    except ValueError:
                        print("error: channel number must be an integer")

            elif cmd == "create-channel":
                if len(parts) < 2:
                    print("usage: create-channel <name>")
                else:
                    name = " ".join(parts[1:]).strip('"')
                    cmd_create_channel(session, name)

            elif cmd == "show":
                display_state(session)

            elif cmd == "help":
                print("available commands:")
                print("  new-network --name <name> --device <device>")
                print("  switch <n>")
                print("  send <message>")
                print("  sync --ticks <n>")
                print("  set-auto-tick <n>")
                print("  select-channel <n>")
                print("  create-channel <name>")
                print("  show")
                print("  quit")

            else:
                print(f"unknown command: {cmd}")
                print("type 'help' for available commands")

        except KeyboardInterrupt:
            print()
            cmd_quit(session)
        except Exception as e:
            print(f"error: {e}")
            import traceback
            traceback.print_exc()


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(description="POC-6 CLI")
    parser.add_argument("--interactive", "-i", action="store_true", help="Run in interactive mode")
    args = parser.parse_args()

    session = CLISession()
    session.initialize_database()

    if args.interactive:
        run_interactive(session)
    else:
        print("non-interactive mode not yet implemented")
        print("use --interactive for now")
        sys.exit(1)


if __name__ == "__main__":
    main()
