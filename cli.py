#!/usr/bin/env python3
"""
POC-6 CLI - Interactive and non-interactive command-line interface.

This CLI uses ONLY event functions from events/ modules - no direct database access.
Same constraints as scenario tests - acts like an API client.
"""

# ============================================================================
# IMPORTANT: CLI FRONTEND RULES
# ============================================================================
#
# This CLI is a "dumb frontend" - it ONLY uses event module functions.
# It NEVER queries the database directly.
#
# RULES:
#
# 1. ❌ NO SQL QUERIES IN CLI
#    - Never use db.execute() or db.execute_returning()
#    - Never import or use SQL files
#    - The CLI doesn't know about database schema
#
# 2. ✅ ONLY USE EVENT MODULE FUNCTIONS (the API)
#    - from events.identity import user, peer, network
#    - from events.content import channel, message
#    - from events.group import group_member
#    - These are the ONLY way to interact with data
#
# 3. 🎯 LIST FUNCTIONS SHOULD RETURN COMPLETE DATA
#    - If the CLI displays a list, the backend list function should
#      return ALL data needed to display that list
#    - NO additional queries per item (N+1 problem)
#    - Example: message.list() should include author_name,
#      not just author_id
#
# 4. 📊 WHEN TO ADD BACKEND FUNCTIONS
#    - If you find yourself writing SQL in CLI → ADD IT TO BACKEND
#    - If you're doing lookups in a loop → FIX THE LIST FUNCTION
#    - If you're joining tables in CLI → WRONG LAYER
#
# These rules ensure:
# - Frontend stays simple and maintainable
# - Backend is reusable by other frontends (mobile, web, etc.)
# - All business logic lives in one place (events/ modules)
# - Tests can mock the API layer cleanly
#
# ============================================================================

import sqlite3
import sys
import argparse
import logging
import shlex
from typing import Optional, Dict, List, Any

# Configure logging BEFORE importing any modules that use logging
# Logging levels (principled tiers):
#   --quiet:   CRITICAL only (system failures)
#   default:   WARNING+ (problems worth noting)
#   --verbose: DEBUG+ (full diagnostic output)
#
# Note: Some backend modules misuse ERROR for info messages.
# We suppress those at WARNING level by default to keep CLI clean.
_verbose = '--verbose' in sys.argv or '-v' in sys.argv
_quiet = '--quiet' in sys.argv or '-q' in sys.argv

if _verbose:
    _log_level = logging.DEBUG
elif _quiet:
    _log_level = logging.CRITICAL
else:
    _log_level = logging.WARNING

logging.basicConfig(level=_log_level, format='%(name)s: %(message)s')

# Backend modules misuse log levels (ERROR for info, etc.)
# Suppress them unless verbose mode is explicitly requested
if not _verbose:
    _noisy_modules = ['events', 'crypto', 'store', 'tick', 'sync', 'queues', 'db']
    for name in _noisy_modules:
        logging.getLogger(name).setLevel(logging.CRITICAL)

from db import Database
import schema
import tick

# Import event functions (this is our API)
from events.identity import user, peer, invite, network
from events.content import channel, message, message_deletion
from events.group import group_member, group_key, group_prekey


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
        self.invites: List[Dict[str, Any]] = []  # List of invite links for convenience

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

    def get_invite_by_number(self, n: int) -> Optional[str]:
        """Get invite link by number (1-indexed)."""
        if 1 <= n <= len(self.invites):
            return self.invites[n - 1]['link']
        return None

    def add_invite(self, link: str, created_by: str):
        """Add an invite link to the session."""
        self.invites.append({
            'link': link,
            'created_by': created_by,
            'created_at': self.current_time_ms
        })

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
    """Display the complete state with all sections."""
    display_accounts(session)
    print()
    display_invites(session)
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

        # Query backend for network_id (may not be cached in account)
        if account.network_id:
            network_id = account.network_id
        else:
            network_info = network.get_for_peer(account.peer_id, account.peer_id, session.db)
            network_id = network_info['network_id'] if network_info else None
        short_net = network_id[:6] if network_id else "???"

        print(f"  {i}. {selected} {account.full_name} - user_{short_user}, peer_{short_peer}, network_{short_net}")


def display_invites(session: CLISession):
    """Display INVITES section (for convenience only)."""
    print("INVITES (for convenience only):")
    if not session.invites:
        print("  (no invites)")
        return

    for i, inv in enumerate(session.invites, 1):
        created_by = inv['created_by']
        link_preview = inv['link'][:60] + "..." if len(inv['link']) > 60 else inv['link']
        print(f"  {i}. created by {created_by} - {link_preview}")


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
                # Name is already in the data from list_members()
                username = member.get('name', '???')
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
    messages = message.list(session.selected_channel_id, account.peer_id, session.db)
    if not messages:
        print("  (no messages)")
        return

    for msg in messages:
        # Author name is already in the data from message.list()
        author_name = msg.get('author_name', '???')
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

    try:
        result = channel.create(
            name=name,
            peer_id=account.peer_id,
            peer_shared_id=account.peer_shared_id,
            t_ms=session.current_time_ms,
            db=session.db,
            group_id=all_users_group_id
        )
    except ValueError as e:
        if "not authorized" in str(e).lower() or "admin" in str(e).lower():
            print("✗ only admins can create channels")
        else:
            print(f"✗ {e}")
        return

    session.db.commit()
    session.current_time_ms += 100

    print(f"✓ created channel #{name}")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_create_invite(session: CLISession):
    """Create an invite link for the current network (admin only)."""
    account = session.get_selected_account()

    if not account.network_id:
        print("✗ no network joined")
        print("  hint: use 'switch' to select an account that has joined a network")
        return

    try:
        invite_id, invite_link, invite_data = invite.create(
            peer_id=account.peer_id,
            t_ms=session.current_time_ms,
            db=session.db
        )
    except ValueError as e:
        if "not an admin" in str(e).lower() or "admin" in str(e).lower():
            print("✗ only admins can create invites")
        else:
            print(f"✗ {e}")
        return

    session.db.commit()
    session.current_time_ms += 100

    # Store in invites list for convenient reference
    session.add_invite(invite_link, account.user_name)
    invite_num = len(session.invites)

    # Store the invite link for --join-with-last-invite shortcut
    session.last_invite_link = invite_link

    print(f"✓ created invite #{invite_num}")
    print(f"  use: new-peer --name <name> --device <device> --invite {invite_num}")
    print()


def cmd_new_peer(session: CLISession, name: str, device: str, invite_ref: str):
    """Create a new peer and join a network via invite link or number."""
    # Resolve invite reference (number or full link)
    invite_link = invite_ref
    if invite_ref.isdigit():
        invite_num = int(invite_ref)
        resolved_link = session.get_invite_by_number(invite_num)
        if not resolved_link:
            print(f"✗ invite #{invite_num} not found")
            return
        invite_link = resolved_link
        print(f"  using invite #{invite_num}")

    # Create the peer first
    peer_id = peer.create(t_ms=session.current_time_ms, db=session.db)

    session.db.commit()
    session.current_time_ms += 100

    # Join the network
    result = user.join(
        peer_id=peer_id,
        invite_link=invite_link,
        name=name,
        t_ms=session.current_time_ms,
        db=session.db
    )

    session.db.commit()
    session.current_time_ms += 100

    # Create account context
    account = AccountContext(
        user_name=name.lower(),
        device_name=device.lower(),
        peer_id=result['peer_id'],
        peer_shared_id=result['peer_shared_id']
    )
    account.user_id = result['user_id']
    account.network_id = result.get('network_id')  # From invite data

    session.add_account(account)
    session.selected_account = account.full_name

    # Auto-select first channel if available
    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)
    if channels:
        session.selected_channel_id = channels[0]['channel_id']

    account_num = list(session.accounts.keys()).index(account.full_name) + 1

    print(f"✓ created peer and joined network as {name.lower()}")
    print(f"✓ selected account #{account_num}: {account.full_name}")
    if channels:
        print(f"✓ selected channel #1: #{channels[0]['name']}")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_list_accounts(session: CLISession):
    """List all accounts in the session."""
    if not session.accounts:
        print("no accounts")
        return

    account_list = list(session.accounts.values())
    for i, account in enumerate(account_list, 1):
        selected = "*" if account.full_name == session.selected_account else " "
        print(f"  {i}. {selected} {account.full_name}")


def cmd_list_channels(session: CLISession):
    """List all channels for the selected account."""
    if not session.selected_account:
        print("error: no account selected")
        return

    account = session.get_selected_account()
    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)

    if not channels:
        print("no channels")
        return

    for i, ch in enumerate(channels, 1):
        selected = "*" if ch['channel_id'] == session.selected_channel_id else " "
        print(f"  {i}. {selected} #{ch['name']}")


def cmd_list_users(session: CLISession):
    """List all users in the network for the selected account."""
    if not session.selected_account:
        print("error: no account selected")
        return

    account = session.get_selected_account()
    if not account.network_id:
        # Try to get from backend
        network_info = network.get_for_peer(account.peer_id, account.peer_id, session.db)
        if not network_info:
            print("error: not in a network")
            return
        network_id = network_info['network_id']
    else:
        network_id = account.network_id

    all_users_group_id = network.get_all_users_group_id(network_id, account.peer_id, session.db)
    members = group_member.list_members(all_users_group_id, account.peer_id, session.db)

    if not members:
        print("no users")
        return

    for i, member in enumerate(members, 1):
        # Name is already in the data from list_members()
        username = member.get('name', '???')
        print(f"  {i}. {username}")


def cmd_time(session: CLISession):
    """Show current simulation time."""
    print(f"{session.current_time_ms}ms")


def cmd_quit(session: CLISession):
    """Quit the CLI."""
    print("goodbye!")
    sys.exit(0)


def cmd_keys(session: CLISession, summary: bool = False):
    """Display key state for forward secrecy demo."""
    account = session.get_selected_account()

    # Get group keys
    keys = group_key.list(account.peer_id, session.db)

    # Get prekeys
    prekeys = group_prekey.list(account.peer_id, session.current_time_ms, session.db)

    if summary:
        display_keys_summary(account, keys, prekeys)
    else:
        display_keys_full(account, keys, prekeys)


def display_keys_full(account: AccountContext, keys: list, prekeys: list):
    """Display full key state."""
    print(f"KEYS ({account.full_name}):")

    print("  group_keys:")
    if not keys:
        print("    (no keys)")
    else:
        for i, k in enumerate(keys, 1):
            key_id_short = k['key_id'][:10]
            status = k['status']
            msg_count = k['message_count']
            print(f"    {i}. key_{key_id_short} - {status} ({msg_count} messages)")

    print()
    print("  prekeys:")
    if not prekeys:
        print("    (no prekeys)")
    else:
        for i, pk in enumerate(prekeys, 1):
            prekey_id_short = pk['prekey_id'][:10]
            status = pk['status']
            key_count = pk['group_key_count']
            print(f"    {i}. prekey_{prekey_id_short} - {status} ({key_count} group_keys)")


def display_keys_summary(account: AccountContext, keys: list, prekeys: list):
    """Display summary key state."""
    print(f"KEYS ({account.full_name}):")

    active_keys = sum(1 for k in keys if k['status'] == 'active')
    pending_keys = sum(1 for k in keys if k['status'] == 'pending_purge')

    active_prekeys = sum(1 for p in prekeys if p['status'] == 'active')
    pending_prekeys = sum(1 for p in prekeys if p['status'] == 'pending_purge')

    print(f"  group_keys: {active_keys} active, {pending_keys} pending_purge")
    print(f"  prekeys: {active_prekeys} active, {pending_prekeys} pending_purge")


def cmd_delete_message(session: CLISession, message_num: int):
    """Delete a message by number."""
    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("✗ no channel selected")
        return

    # Get messages to find the one to delete
    messages = message.list(session.selected_channel_id, account.peer_id, session.db)

    if not (1 <= message_num <= len(messages)):
        print(f"✗ message #{message_num} not found")
        return

    msg = messages[message_num - 1]
    message_id = msg['message_id']

    deletion_id = message_deletion.create(
        peer_id=account.peer_id,
        message_id=message_id,
        t_ms=session.current_time_ms,
        db=session.db
    )

    session.db.commit()
    session.current_time_ms += 100

    print(f"✓ deleted message")
    print(f"✓ marked key for purging")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_purge_keys(session: CLISession):
    """Run forward secrecy purge cycle."""
    account = session.get_selected_account()

    stats = message_deletion.run_message_purge_cycle(
        peer_id=account.peer_id,
        t_ms=session.current_time_ms,
        db=session.db
    )

    session.db.commit()
    session.current_time_ms += 100

    if stats['messages_rekeyed'] > 0:
        print(f"✓ rekeyed {stats['messages_rekeyed']} messages")
    if stats['keys_purged'] > 0:
        print(f"✓ purged {stats['keys_purged']} keys")
    if stats.get('prekeys_purged', 0) > 0:
        print(f"✓ purged {stats['prekeys_purged']} prekeys")
    if stats['errors']:
        for err in stats['errors']:
            print(f"⚠ {err}")
    if stats['messages_rekeyed'] == 0 and stats['keys_purged'] == 0:
        print("✓ no keys to purge")
    print()

    session.run_auto_tick()
    display_state(session)


# ============================================================================
# COMMAND EXECUTION
# ============================================================================

def execute_command(session: CLISession, line: str, show_prompt: bool = True) -> bool:
    """Execute a single command line. Returns False if should quit."""
    line = line.strip()
    if not line:
        return True

    try:
        parts = shlex.split(line)
    except ValueError as e:
        print(f"error: {e}")
        return True
    cmd = parts[0]

    try:
        if cmd == "quit" or cmd == "exit":
            if show_prompt:
                print("goodbye!")
            return False

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

        elif cmd == "create-invite":
            cmd_create_invite(session)

        elif cmd == "new-peer":
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--name", required=True)
            parser.add_argument("--device", required=True)
            parser.add_argument("--invite", required=True)
            try:
                args = parser.parse_args(parts[1:])
                cmd_new_peer(session, args.name, args.device, args.invite)
            except SystemExit:
                print("usage: new-peer --name <name> --device <device> --invite <link>")

        elif cmd == "show":
            display_state(session)

        elif cmd == "list-accounts":
            cmd_list_accounts(session)

        elif cmd == "list-channels":
            cmd_list_channels(session)

        elif cmd == "list-users":
            cmd_list_users(session)

        elif cmd == "keys":
            summary = "--summary" in parts
            cmd_keys(session, summary=summary)

        elif cmd == "delete-message":
            if len(parts) < 2:
                print("usage: delete-message <n>")
            else:
                try:
                    cmd_delete_message(session, int(parts[1]))
                except ValueError:
                    print("error: message number must be an integer")

        elif cmd == "purge-keys":
            cmd_purge_keys(session)

        elif cmd == "time":
            cmd_time(session)

        elif cmd == "help":
            print("available commands:")
            print("  new-network --name <name> --device <device>")
            print("  new-peer --name <name> --device <device> --invite <n|link>")
            print("  switch <n>")
            print("  send <message>")
            print("  sync --ticks <n>")
            print("  set-auto-tick <n>")
            print("  select-channel <n>")
            print("  create-channel <name>")
            print("  create-invite")
            print("  list-accounts")
            print("  list-channels")
            print("  list-users")
            print("  keys [--summary]")
            print("  delete-message <n>")
            print("  purge-keys")
            print("  time")
            print("  show")
            print("  quit")

        else:
            print(f"unknown command: {cmd}")
            print("type 'help' for available commands")

        return True

    except KeyboardInterrupt:
        print()
        return False
    except Exception as e:
        print(f"error: {e}")
        if _verbose:
            import traceback
            traceback.print_exc()
        return True


# ============================================================================
# INTERACTIVE AND NON-INTERACTIVE MODES
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
            if not execute_command(session, line, show_prompt=True):
                break
        except KeyboardInterrupt:
            print()
            print("goodbye!")
            break
        except EOFError:
            print()
            print("goodbye!")
            break


def run_non_interactive(session: CLISession):
    """Run non-interactive mode, reading commands from stdin."""
    import sys

    # Show initial state
    display_state(session)
    print()

    for line in sys.stdin:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        print(f"> {line}")
        if not execute_command(session, line, show_prompt=False):
            break


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(description="POC-6 CLI")
    parser.add_argument("--interactive", "-i", action="store_true", help="Run in interactive mode")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress all but critical logs")
    args = parser.parse_args()

    # Logging already configured at import time based on sys.argv

    session = CLISession()
    session.initialize_database()

    if args.interactive:
        run_interactive(session)
    else:
        run_non_interactive(session)


if __name__ == "__main__":
    main()
