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
from events.identity import user, peer, invite, network, user_removed, peer_shared
from events.content import channel, message, message_deletion, message_reaction, message_update, channel_update
import purge_expired
from events.group import group_member, group_key, group_prekey, group
import network_config


class EventLog:
    """Captures human-readable event descriptions during command execution.

    When enabled, logs events in JSON-lite format:
        → event_type {field=value, field=<substituted>}

    Angle brackets denote human-readable substitutions for event IDs.
    """

    def __init__(self):
        self.entries: List[str] = []
        self.enabled: bool = False

    def clear(self):
        """Clear log entries before each command."""
        self.entries = []

    def log(self, event_type: str, **fields):
        """Log an event with key=value fields.

        Use angle brackets in values to denote ID substitutions:
            log("message", channel="<#general>", author="<alice>", content="hello")
        """
        if self.enabled:
            field_strs = [f"{k}={v}" for k, v in fields.items()]
            self.entries.append(f"  → {event_type} {{{', '.join(field_strs)}}}")

    def display(self):
        """Display log entries if any exist and logging is enabled."""
        if self.enabled and self.entries:
            print("☰ event log:")
            for entry in self.entries:
                print(entry)
            print()


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
        self.event_log: EventLog = EventLog()  # Event log for debugging/visibility

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

# ============================================================================
# FORMATTING UTILITIES
# ============================================================================

def format_duration_short(ms: int) -> str:
    """Format milliseconds as short duration: 7d, 12h, 30m"""
    if ms <= 0:
        return ""
    days = ms // (24 * 60 * 60 * 1000)
    if days >= 1:
        return f"{days}d"
    hours = ms // (60 * 60 * 1000)
    if hours >= 1:
        return f"{hours}h"
    minutes = ms // (60 * 1000)
    if minutes >= 1:
        return f"{minutes}m"
    return "<1m"


def format_expires_in(expires_at_ms: int, current_time_ms: int) -> str:
    """Format time remaining until expiration: 'expires in: 6d 23h'"""
    remaining = expires_at_ms - current_time_ms
    if remaining <= 0:
        return "(expired)"
    days = remaining // (24 * 60 * 60 * 1000)
    hours = (remaining % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000)
    if days > 0:
        return f"(expires in: {days}d {hours}h)"
    minutes = (remaining % (60 * 60 * 1000)) // (60 * 1000)
    if hours > 0:
        return f"(expires in: {hours}h {minutes}m)"
    return f"(expires in: {minutes}m)"


# ============================================================================
# EVENT LOG HELPERS
# ============================================================================


def get_channel_name(session: CLISession, channel_id: str) -> str:
    """Get channel name for event log display."""
    if not session.selected_account:
        return "#???"
    account = session.get_selected_account()
    ch = channel.get_by_id(channel_id, account.peer_id, session.db)
    return f"#{ch['name']}" if ch else "#???"


def get_message_preview(content: str, max_len: int = 22) -> str:
    """Get truncated message preview for event log display."""
    if len(content) <= max_len:
        return f'"{content}"'
    return f'"{content[:max_len-3]}..."'


# ============================================================================
# DISPLAY FUNCTIONS
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
            disappearing = ch.get('disappearing_time_ms', 0)
            if disappearing > 0:
                disappearing_str = f" (disappearing: {format_duration_short(disappearing)})"
            else:
                disappearing_str = ""
            print(f"    {i}. {selected} #{ch['name']}{disappearing_str}")
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

    for i, msg in enumerate(messages, 1):
        # Author name is already in the data from message.list()
        author_name = msg.get('author_name', '???')
        timestamp = msg.get('created_at', 0)
        content = msg.get('content', '')
        edited_at = msg.get('edited_at', 0)
        ttl_ms = msg.get('ttl_ms', 0)

        # Show edit indicator if message was edited
        edit_indicator = ""
        if edited_at:
            edit_indicator = f" (edited at {edited_at}ms)"

        # Show expiration if message has TTL
        expires_indicator = ""
        if ttl_ms > 0:
            expires_indicator = f" {format_expires_in(ttl_ms, session.current_time_ms)}"

        print(f"  {i}. [{timestamp}ms] {author_name}: {content}{edit_indicator}{expires_indicator}")

        # Display reactions if any
        reactions = msg.get('reactions', [])
        if reactions:
            reaction_strs = []
            for reaction in reactions:
                emoji = reaction.get('emoji', '?')
                count = reaction.get('count', 0)
                reaction_strs.append(f"{emoji}({count})")
            print(f"     reactions: {' '.join(reaction_strs)}")


# ============================================================================
# COMMANDS
# ============================================================================

def cmd_new_network(session: CLISession, network_name: str, username: str, devicename: str):
    """Create a new network and first account."""
    # Canonicalize: lowercase for consistent storage and display
    devicename = devicename.lower()
    result = user.new_network(name=username, t_ms=session.current_time_ms, db=session.db, device_name=devicename, network_name=network_name)

    # Create account context
    account = AccountContext(
        user_name=username.lower(),
        device_name=devicename,
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

    # Log events created
    session.event_log.log("peer", name=username.lower(), device=devicename)
    session.event_log.log("network", signed_by="<self>")
    session.event_log.log("user_invite", type="bootstrap")
    session.event_log.log("user", name=username.lower(), invite="<bootstrap>")
    session.event_log.log("peer_shared", user=f"<{username.lower()}>", device=devicename)
    session.event_log.log("admin_grant", user=f"<{username.lower()}>")
    session.event_log.log("group", name="all_users")
    session.event_log.log("channel", name="general", group="<all_users>")

    account_num = list(session.accounts.keys()).index(account.full_name) + 1

    print(f"✓ created network: {network_name}")
    print(f"✓ selected account #{account_num}: {account.full_name}")
    print(f"✓ selected channel #1: #general")
    session.event_log.display()

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
    if not msg or not msg.strip():
        print("✗ message cannot be empty or whitespace-only")
        return

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

    # Log event
    channel_name = get_channel_name(session, session.selected_channel_id)
    session.event_log.log("message", channel=f"<{channel_name}>", author=f"<{account.user_name}>", content=f'"{msg}"')

    print("✓ sent message")
    session.event_log.display()

    session.run_auto_tick()
    display_state(session)


def cmd_tick(session: CLISession, count: int):
    """Run manual ticks to advance simulation time."""
    print(f"⟳ ticking {count}...")
    start_t = session.current_time_ms
    for _ in range(count):
        session.current_time_ms += 100
        tick.tick(t_ms=session.current_time_ms, db=session.db)
    print(f"✓ ticked (t={start_t}ms -> {session.current_time_ms}ms)")
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
    if not name or not name.strip():
        print("✗ channel name cannot be empty or whitespace-only")
        return

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

    # Log event
    session.event_log.log("channel", name=name, group="<all_users>")

    print(f"✓ created channel #{name}")
    session.event_log.display()

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

    # Log event
    session.event_log.log("user_invite", created_by=f"<{account.user_name}>")

    # Store in invites list for convenient reference
    session.add_invite(invite_link, account.user_name)
    invite_num = len(session.invites)

    # Store the invite link for --join-with-last-invite shortcut
    session.last_invite_link = invite_link

    print(f"✓ created invite #{invite_num}")
    print(f"  use: accept-invite --username <name> --devicename <device> --invite {invite_num}")
    session.event_log.display()


def cmd_create_link_invite(session: CLISession):
    """Create a device linking invite for the current user (admin only).

    This creates a link that allows adding another device to the currently
    selected user's account. The user_id is embedded in the invite.
    """
    account = session.get_selected_account()

    if not account.network_id:
        print("✗ no network joined")
        print("  hint: use 'switch' to select an account that has joined a network")
        return

    if not account.user_id:
        print("✗ no user_id for current account")
        return

    try:
        invite_id, invite_link, invite_data = invite.create(
            peer_id=account.peer_id,
            t_ms=session.current_time_ms,
            db=session.db,
            mode='peer',
            user_id=account.user_id
        )
    except ValueError as e:
        if "not an admin" in str(e).lower() or "admin" in str(e).lower():
            print("✗ only admins can create device linking invites")
        else:
            print(f"✗ {e}")
        return

    session.db.commit()
    session.current_time_ms += 100

    # Log event
    session.event_log.log("device_invite", created_by=f"<{account.user_name}>", for_user=f"<{account.user_name}>")

    # Store in invites list for convenient reference
    session.add_invite(invite_link, f"{account.user_name} (link)")
    invite_num = len(session.invites)

    print(f"✓ created device link invite #{invite_num} for user: {account.user_name}")
    print(f"  use: accept-link --devicename <device> --invite {invite_num}")
    session.event_log.display()


def cmd_link_device(session: CLISession, devicename: str, invite_ref: str):
    """Link a new device to an existing user via link invite.

    Unlike 'join', this does NOT create a new user. The user_id comes from
    the invite itself (embedded by the admin who created it).
    """
    # Canonicalize: lowercase for consistent storage and display
    devicename = devicename.lower()

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

    # Validate this is a device linking invite (quiet://link/...)
    if not invite_link.startswith('quiet://link/'):
        print("✗ this is not a device linking invite")
        print("  hint: use 'accept-invite' for network join invites (quiet://invite/...)")
        print("  hint: use 'accept-link' for device linking invites (quiet://link/...)")
        return

    # Create the peer first
    peer_id = peer.create(t_ms=session.current_time_ms, db=session.db)

    session.db.commit()
    session.current_time_ms += 100

    # Accept the invite - this parses the link and extracts user_id
    try:
        accepted = invite.accept(peer_id, invite_link, t_ms=session.current_time_ms, db=session.db)
    except ValueError as e:
        print(f"✗ failed to accept invite: {e}")
        return

    if accepted['mode'] != 'peer':
        print("✗ this is not a device linking invite")
        print("  hint: use 'accept-invite' for network join invites")
        return

    user_id = accepted.get('user_id')
    if not user_id:
        print("✗ invite does not contain user_id")
        return

    session.db.commit()
    session.current_time_ms += 100

    # Complete the peer linking via peer_shared.join()
    result = peer_shared.join(
        peer_id=peer_id,
        peer_invite_id=accepted['invite_id'],
        peer_invite_private_key=accepted['invite_private_key'],
        user_id=user_id,
        prekey_id=accepted['invite_prekey_id'],
        t_ms=session.current_time_ms,
        db=session.db,
        device_name=devicename
    )

    session.db.commit()
    session.current_time_ms += 100

    # Get username and network_id from an existing account with the same user_id
    # (since link-device links to an existing user, there should be another device already)
    username = None
    network_id = None
    for existing_account in session.accounts.values():
        if existing_account.user_id == user_id:
            username = existing_account.user_name
            network_id = existing_account.network_id
            break

    # Fallback: try to get from database (might work after sync)
    if not username:
        from db import create_safe_db
        safedb = create_safe_db(session.db, recorded_by=peer_id)
        user_row = safedb.query_one(
            "SELECT name FROM users WHERE user_id = ? AND recorded_by = ? LIMIT 1",
            (user_id, peer_id)
        )
        username = user_row['name'] if user_row else "unknown"

    if not network_id:
        from db import create_safe_db
        safedb = create_safe_db(session.db, recorded_by=peer_id)
        network_row = safedb.query_one(
            "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
            (peer_id,)
        )
        network_id = network_row['network_id'] if network_row else None

    # Create account context
    account = AccountContext(
        user_name=username.lower() if username else "unknown",
        device_name=devicename,
        peer_id=result['peer_id'],
        peer_shared_id=result['peer_shared_id']
    )
    account.user_id = user_id
    account.network_id = network_id

    session.add_account(account)
    session.selected_account = account.full_name

    # Auto-select first channel if available
    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)
    if channels:
        session.selected_channel_id = channels[0]['channel_id']

    account_num = list(session.accounts.keys()).index(account.full_name) + 1

    print(f"✓ linked device to existing user: {username}")
    print(f"✓ selected account #{account_num}: {account.full_name}")
    if channels:
        print(f"✓ selected channel #1: #{channels[0]['name']}")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_join(session: CLISession, username: str, devicename: str, invite_ref: str):
    """Join a network as a NEW user via invite link or number."""
    # Canonicalize: lowercase for consistent storage and display
    devicename = devicename.lower()
    # Resolve invite reference (number or full link)
    invite_link = invite_ref
    invite_display = None  # For event log
    if invite_ref.isdigit():
        invite_num = int(invite_ref)
        resolved_link = session.get_invite_by_number(invite_num)
        if not resolved_link:
            print(f"✗ invite #{invite_num} not found")
            return
        invite_link = resolved_link
        invite_display = f"#{invite_num}"
        print(f"  using invite #{invite_num}")

    # Create the peer first
    peer_id = peer.create(t_ms=session.current_time_ms, db=session.db)

    session.db.commit()
    session.current_time_ms += 100

    # Join the network as a new user
    result = user.join(
        peer_id=peer_id,
        invite_link=invite_link,
        name=username,
        t_ms=session.current_time_ms,
        db=session.db,
        device_name=devicename
    )

    session.db.commit()
    session.current_time_ms += 100

    # Create account context
    account = AccountContext(
        user_name=username.lower(),
        device_name=devicename,
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

    # Log events created - use invite number if available, otherwise truncate invite_id from result
    if not invite_display:
        invite_id = result.get('invite_id', '')
        invite_display = invite_id[:8] + "..." if invite_id else "link"
    session.event_log.log("peer", name=username.lower(), device=devicename)
    session.event_log.log("invite_accepted", invite=f"<{invite_display}>")
    session.event_log.log("user", name=username.lower(), invite=f"<{invite_display}>")
    session.event_log.log("peer_shared", user=f"<{username.lower()}>", device=devicename)

    print(f"✓ joined network as new user: {username.lower()}")
    print(f"✓ selected account #{account_num}: {account.full_name}")
    if channels:
        print(f"✓ selected channel #1: #{channels[0]['name']}")
    session.event_log.display()

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
        disappearing = ch.get('disappearing_time_ms', 0)
        if disappearing > 0:
            disappearing_str = f" (disappearing: {format_duration_short(disappearing)})"
        else:
            disappearing_str = ""
        print(f"  {i}. {selected} #{ch['name']}{disappearing_str}")


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


def cmd_list_messages(session: CLISession):
    """List messages in the selected channel."""
    if not session.selected_account:
        print("error: no account selected")
        return

    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("error: no channel selected")
        return

    messages = message.list(session.selected_channel_id, account.peer_id, session.db)
    if not messages:
        print("no messages")
        return

    for i, msg in enumerate(messages, 1):
        author_name = msg.get('author_name', '???')
        content = msg.get('content', '')
        print(f"  {i}. {author_name}: {content}")


def cmd_time(session: CLISession):
    """Show current simulation time."""
    print(f"{session.current_time_ms}ms")


def cmd_set_disappearing(session: CLISession, days: int | None = None, time_ms: int | None = None, off: bool = False):
    """Set disappearing messages time for selected channel."""
    if not session.selected_channel_id:
        print("Error: No channel selected. Use 'channel <n>' first.")
        return

    account = session.get_selected_account()

    # Check admin
    if not invite.is_admin(account.peer_shared_id, account.peer_id, session.db):
        print("Error: Only admins can change disappearing messages settings")
        return

    # Calculate TTL
    if off:
        ttl_ms = 0
    elif days:
        ttl_ms = days * 24 * 60 * 60 * 1000
    else:
        ttl_ms = time_ms

    # Get channel name for display
    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)
    channel_name = next((ch['name'] for ch in channels if ch['channel_id'] == session.selected_channel_id), '???')

    try:
        channel_update.create(
            channel_id=session.selected_channel_id,
            peer_id=account.peer_id,
            peer_shared_id=account.peer_shared_id,
            t_ms=session.current_time_ms,
            db=session.db,
            new_disappearing_time_ms=ttl_ms
        )
        session.db.commit()
        session.current_time_ms += 100

        if ttl_ms == 0:
            print(f"Turned off disappearing messages for #{channel_name} (affects new messages only)")
        else:
            print(f"Set disappearing messages to {format_duration_short(ttl_ms)} for #{channel_name} (affects new messages only)")
        print()

        session.run_auto_tick()
        display_state(session)
    except ValueError as e:
        print(f"Error: {e}")


def cmd_fast_forward(session: CLISession, days: int):
    """Fast-forward simulation time by days."""
    if days < 0:
        print("error: days must be non-negative")
        return

    ms = days * 24 * 60 * 60 * 1000
    session.current_time_ms += ms

    # Run purge to delete expired messages
    purge_expired.run_purge_expired_for_all_peers(session.current_time_ms, session.db)
    session.db.commit()

    print(f"Fast-forwarded {days} day{'s' if days != 1 else ''} (current time: {session.current_time_ms}ms)")
    print()
    display_state(session)


def cmd_toggle_log(session: CLISession):
    """Toggle event logging on/off."""
    session.event_log.enabled = not session.event_log.enabled
    state = "ON" if session.event_log.enabled else "OFF"
    print(f"event log: {state}")


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

    # Get channel -> key mappings (single query with JOIN)
    channels_with_keys = channel.list_channels_with_keys(recorded_by=account.peer_id, db=session.db)
    channel_keys = [{'name': ch['name'], 'key_id': ch['key_id']} for ch in channels_with_keys]

    if summary:
        display_keys_summary(account, keys, prekeys, channel_keys)
    else:
        display_keys_full(account, keys, prekeys, channel_keys)


def display_keys_full(account: AccountContext, keys: list, prekeys: list, channel_keys: list):
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

    print()
    print("  channels:")
    if not channel_keys:
        print("    (no channels)")
    else:
        for i, ck in enumerate(channel_keys, 1):
            key_id_short = ck['key_id'][:10] if ck['key_id'] else "(none)"
            print(f"    {i}. #{ck['name']:16} key_{key_id_short}")


def display_keys_summary(account: AccountContext, keys: list, prekeys: list, channel_keys: list):
    """Display summary key state."""
    print(f"KEYS ({account.full_name}):")

    active_keys = sum(1 for k in keys if k['status'] == 'active')
    pending_keys = sum(1 for k in keys if k['status'] == 'pending_purge')

    active_prekeys = sum(1 for p in prekeys if p['status'] == 'active')
    pending_prekeys = sum(1 for p in prekeys if p['status'] == 'pending_purge')

    print(f"  group_keys: {active_keys} active, {pending_keys} pending_purge")
    print(f"  prekeys: {active_prekeys} active, {pending_prekeys} pending_purge")
    print(f"  channels: {len(channel_keys)}")


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

    # Log event
    content_preview = get_message_preview(msg.get('content', ''))
    channel_name = get_channel_name(session, session.selected_channel_id)
    session.event_log.log("message_deletion", content=content_preview, channel=f"<{channel_name}>")

    print(f"✓ deleted message")
    print(f"✓ marked key for purging")
    session.event_log.display()

    session.run_auto_tick()
    display_state(session)


def cmd_edit_message(session: CLISession, message_num: int, new_content: str):
    """Edit a message by number (only own messages)."""
    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("✗ no channel selected")
        return

    # Get messages to find the one to edit
    messages = message.list(session.selected_channel_id, account.peer_id, session.db)

    if not (1 <= message_num <= len(messages)):
        print(f"✗ message #{message_num} not found")
        return

    msg = messages[message_num - 1]
    message_id = msg['message_id']

    try:
        update_id = message_update.create(
            message_id=message_id,
            new_content=new_content,
            peer_id=account.peer_id,
            t_ms=session.current_time_ms,
            db=session.db
        )
    except ValueError as e:
        if "author" in str(e).lower():
            print(f"✗ only the message author can edit")
        elif "empty" in str(e).lower():
            print(f"✗ message content cannot be empty")
        else:
            print(f"✗ {e}")
        return

    session.db.commit()
    session.current_time_ms += 100

    # Log event
    content_preview = get_message_preview(msg.get('content', ''))
    channel_name = get_channel_name(session, session.selected_channel_id)
    session.event_log.log("message_update", content=content_preview, channel=f"<{channel_name}>", new_content=f'"{new_content}"')

    print(f"✓ edited message #{message_num}")
    session.event_log.display()

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


def cmd_remove_user(session: CLISession, user_num: int):
    """Remove a user from the network (admin only)."""
    account = session.get_selected_account()

    # Get all users in the network
    if not account.network_id:
        network_info = network.get_for_peer(account.peer_id, account.peer_id, session.db)
        if not network_info:
            print("✗ not in a network")
            return
        network_id = network_info['network_id']
    else:
        network_id = account.network_id

    all_users_group_id = network.get_all_users_group_id(network_id, account.peer_id, session.db)
    members = group_member.list_members(all_users_group_id, account.peer_id, session.db)

    if not (1 <= user_num <= len(members)):
        print(f"✗ user #{user_num} not found (must be 1-{len(members)})")
        return

    user_to_remove = members[user_num - 1]
    user_id = user_to_remove['user_id']
    username = user_to_remove['name']

    try:
        user_removed.create(
            removed_user_id=user_id,
            removed_by_peer_id=account.peer_shared_id,
            removed_by_local_peer_id=account.peer_id,
            t_ms=session.current_time_ms,
            db=session.db
        )
    except ValueError as e:
        if "authorized" in str(e).lower() or "admin" in str(e).lower():
            print("✗ only admins can remove users")
        else:
            print(f"✗ {e}")
        return

    session.db.commit()
    session.current_time_ms += 100

    print(f"✓ removed user #{user_num}: {username}")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_add_reaction(session: CLISession, message_num: int, emoji: str):
    """Add a reaction (emoji) to a message."""
    if not emoji or not emoji.strip():
        print("✗ reaction cannot be empty or whitespace-only")
        return

    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("✗ no channel selected")
        return

    # Get messages to find the one to react to
    messages = message.list(session.selected_channel_id, account.peer_id, session.db)

    if not (1 <= message_num <= len(messages)):
        print(f"✗ message #{message_num} not found")
        return

    msg = messages[message_num - 1]
    message_id = msg['message_id']

    reaction_id = message_reaction.create(
        peer_id=account.peer_id,
        message_id=message_id,
        emoji=emoji,
        t_ms=session.current_time_ms,
        db=session.db
    )

    session.db.commit()
    session.current_time_ms += 100

    # Log event
    content_preview = get_message_preview(msg.get('content', ''))
    channel_name = get_channel_name(session, session.selected_channel_id)
    session.event_log.log("message_reaction", emoji=emoji, by=f"<{account.user_name}>", content=content_preview, channel=f"<{channel_name}>")

    print(f"✓ added reaction {emoji} to message #{message_num}")
    session.event_log.display()

    session.run_auto_tick()
    display_state(session)


def cmd_remove_reaction(session: CLISession, message_num: int, emoji: str):
    """Remove a reaction (emoji) from a message."""
    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("✗ no channel selected")
        return

    # Get messages to find the one to remove reaction from
    messages = message.list(session.selected_channel_id, account.peer_id, session.db)

    if not (1 <= message_num <= len(messages)):
        print(f"✗ message #{message_num} not found")
        return

    msg = messages[message_num - 1]
    message_id = msg['message_id']

    deletion_id = message_reaction.remove(
        peer_id=account.peer_id,
        message_id=message_id,
        emoji=emoji,
        t_ms=session.current_time_ms,
        db=session.db
    )

    session.db.commit()
    session.current_time_ms += 100

    print(f"✓ removed reaction {emoji} from message #{message_num}")
    print()

    session.run_auto_tick()
    display_state(session)


def cmd_list_reactions(session: CLISession, message_num: int):
    """List all reactions on a message."""
    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("✗ no channel selected")
        return

    # Get messages to find the one to show reactions for
    messages = message.list(session.selected_channel_id, account.peer_id, session.db)

    if not (1 <= message_num <= len(messages)):
        print(f"✗ message #{message_num} not found")
        return

    msg = messages[message_num - 1]
    message_id = msg['message_id']

    # Get reactions for this message
    reactions = message_reaction.list_reactions(message_id, account.peer_id, session.db)

    if not reactions:
        print(f"no reactions on message #{message_num}")
        print()
        return

    print(f"reactions on message #{message_num}:")
    for reaction in reactions:
        emoji = reaction.get('emoji', '?')
        reactors = reaction.get('reactors', [])
        print(f"  {emoji}: {', '.join(reactors)}")
    print()


# ============================================================================
# NETWORK SIMULATION COMMANDS
# ============================================================================

def cmd_network_show(session: CLISession):
    """Show current network simulation configuration."""
    cfg = network_config.get_network_config()

    print("NETWORK SIMULATION:")
    print(f"  latency:      {cfg.latency_ms}ms")
    print(f"  jitter:       {cfg.jitter_ms}ms (stddev)")
    print(f"  packet_loss:  {cfg.packet_loss_rate * 100:.1f}%")
    print(f"  burst_loss:   {cfg.burst_loss_probability * 100:.1f}% (length: {cfg.burst_loss_length})")
    print(f"  max_packet:   {cfg.max_packet_size} bytes")

    if cfg.partitioned_peers:
        print(f"  partitioned:  {', '.join(sorted(cfg.partitioned_peers))}")
    else:
        print(f"  partitioned:  (none)")
    print()


def cmd_network_set(session: CLISession, latency: int = None, jitter: int = None,
                    loss: float = None, burst_prob: float = None, burst_len: int = None):
    """Set network simulation parameters."""
    cfg = network_config.get_network_config()

    changes = []

    if latency is not None:
        if latency < 0:
            print("✗ latency cannot be negative")
            return
        cfg.latency_ms = latency
        changes.append(f"latency={latency}ms")

    if jitter is not None:
        if jitter < 0:
            print("✗ jitter cannot be negative")
            return
        cfg.jitter_ms = jitter
        changes.append(f"jitter={jitter}ms")

    if loss is not None:
        if not (0 <= loss <= 100):
            print("✗ loss must be 0-100%")
            return
        cfg.packet_loss_rate = loss / 100.0
        changes.append(f"loss={loss}%")

    if burst_prob is not None:
        if not (0 <= burst_prob <= 100):
            print("✗ burst probability must be 0-100%")
            return
        cfg.burst_loss_probability = burst_prob / 100.0
        changes.append(f"burst_prob={burst_prob}%")

    if burst_len is not None:
        if burst_len < 1:
            print("✗ burst length must be >= 1")
            return
        cfg.burst_loss_length = burst_len
        changes.append(f"burst_len={burst_len}")

    if changes:
        print(f"✓ network config: {', '.join(changes)}")
    else:
        print("✗ no parameters specified")
        print("  usage: net-set --latency <ms> --jitter <ms> --loss <percent> --burst-prob <percent> --burst-len <n>")


def cmd_network_preset(session: CLISession, preset: str):
    """Apply a network condition preset."""
    presets = {
        'lan': (0, 0, 0, 0, 3),           # Perfect local network
        'broadband': (50, 20, 1, 0, 3),    # Good home internet
        'mobile-4g': (100, 40, 3, 5, 3),   # Mobile 4G
        'mobile-3g': (200, 80, 8, 10, 4),  # Mobile 3G / poor
        'satellite': (300, 50, 2, 5, 3),   # Satellite internet
        'lossy': (50, 20, 15, 20, 5),      # Very lossy network
    }

    if preset not in presets:
        print(f"✗ unknown preset: {preset}")
        print(f"  available: {', '.join(presets.keys())}")
        return

    latency, jitter, loss, burst_prob, burst_len = presets[preset]
    cfg = network_config.get_network_config()
    cfg.latency_ms = latency
    cfg.jitter_ms = jitter
    cfg.packet_loss_rate = loss / 100.0
    cfg.burst_loss_probability = burst_prob / 100.0
    cfg.burst_loss_length = burst_len

    print(f"✓ applied preset '{preset}':")
    print(f"  latency={latency}ms, jitter={jitter}ms, loss={loss}%, burst={burst_prob}%/{burst_len}")


def cmd_partition(session: CLISession, account_num: int):
    """Partition an account (block all network traffic)."""
    account = session.get_account_by_number(account_num)
    if not account:
        print(f"✗ account #{account_num} not found")
        return

    network_config.partition_peer(account.peer_id)
    print(f"✓ partitioned account #{account_num}: {account.full_name}")
    print(f"  (all network traffic blocked)")


def cmd_unpartition(session: CLISession, account_num: int):
    """Unpartition an account (restore network traffic)."""
    account = session.get_account_by_number(account_num)
    if not account:
        print(f"✗ account #{account_num} not found")
        return

    network_config.unpartition_peer(account.peer_id)
    print(f"✓ unpartitioned account #{account_num}: {account.full_name}")
    print(f"  (network traffic restored)")


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

    # Clear event log before each command
    session.event_log.clear()

    try:
        if cmd == "quit" or cmd == "exit":
            if show_prompt:
                print("goodbye!")
            return False

        elif cmd == "new-network":
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--name", required=True)
            parser.add_argument("--username", required=True)
            parser.add_argument("--devicename", required=True)
            try:
                args = parser.parse_args(parts[1:])
                cmd_new_network(session, args.name, args.username, args.devicename)
            except SystemExit:
                print("usage: new-network --name <name> --username <username> --devicename <device>")

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

        elif cmd == "tick":
            if len(parts) < 2:
                print("usage: tick <n>")
            else:
                try:
                    cmd_tick(session, int(parts[1]))
                except ValueError:
                    print("usage: tick <n>")

        elif cmd == "sync":
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--ticks", type=int, required=True)
            try:
                args = parser.parse_args(parts[1:])
                cmd_tick(session, args.ticks)
            except SystemExit:
                print("usage: sync --ticks <n>")

        elif cmd == "auto-tick":
            if len(parts) < 2:
                print("usage: auto-tick <n>")
            else:
                try:
                    cmd_set_auto_tick(session, int(parts[1]))
                except ValueError:
                    print("error: count must be an integer")

        elif cmd == "channel":
            if len(parts) < 2:
                print("usage: channel <n>")
            else:
                try:
                    cmd_select_channel(session, int(parts[1]))
                except ValueError:
                    print("error: channel number must be an integer")

        elif cmd == "new-channel":
            if len(parts) < 2:
                print("usage: new-channel <name>")
            else:
                name = " ".join(parts[1:]).strip('"')
                cmd_create_channel(session, name)

        elif cmd == "invite":
            cmd_create_invite(session)

        elif cmd == "link":
            cmd_create_link_invite(session)

        elif cmd == "accept-invite":
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--username", required=True)
            parser.add_argument("--devicename", required=True)
            parser.add_argument("--invite", required=True)
            try:
                args = parser.parse_args(parts[1:])
                cmd_join(session, args.username, args.devicename, args.invite)
            except SystemExit:
                print("usage: accept-invite --username <username> --devicename <device> --invite <n|link>")

        elif cmd == "accept-link":
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--devicename", required=True)
            parser.add_argument("--invite", required=True)
            try:
                args = parser.parse_args(parts[1:])
                cmd_link_device(session, args.devicename, args.invite)
            except SystemExit:
                print("usage: accept-link --devicename <device> --invite <n|link>")

        elif cmd == "status":
            display_state(session)

        elif cmd == "accounts":
            cmd_list_accounts(session)

        elif cmd == "channels":
            cmd_list_channels(session)

        elif cmd == "users":
            cmd_list_users(session)

        elif cmd == "messages":
            cmd_list_messages(session)

        elif cmd == "keys":
            summary = "--summary" in parts
            cmd_keys(session, summary=summary)

        elif cmd == "delete":
            if len(parts) < 2:
                print("usage: delete <n>")
            else:
                try:
                    cmd_delete_message(session, int(parts[1]))
                except ValueError:
                    print("error: message number must be an integer")

        elif cmd == "edit":
            if len(parts) < 3:
                print("usage: edit <message_num> <new_content>")
            else:
                try:
                    message_num = int(parts[1])
                    new_content = " ".join(parts[2:]).strip('"')
                    cmd_edit_message(session, message_num, new_content)
                except ValueError:
                    print("error: message number must be an integer")

        elif cmd == "purge-keys":
            cmd_purge_keys(session)

        elif cmd == "ban":
            if len(parts) < 2:
                print("usage: ban <n>")
            else:
                try:
                    cmd_remove_user(session, int(parts[1]))
                except ValueError:
                    print("error: user number must be an integer")

        elif cmd == "react":
            if len(parts) < 3:
                print("usage: react <message_num> <emoji>")
            else:
                try:
                    message_num = int(parts[1])
                    emoji = parts[2]
                    cmd_add_reaction(session, message_num, emoji)
                except ValueError:
                    print("error: message number must be an integer")

        elif cmd == "unreact":
            if len(parts) < 3:
                print("usage: unreact <message_num> <emoji>")
            else:
                try:
                    message_num = int(parts[1])
                    emoji = parts[2]
                    cmd_remove_reaction(session, message_num, emoji)
                except ValueError:
                    print("error: message number must be an integer")

        elif cmd == "reactions":
            if len(parts) < 2:
                print("usage: reactions <message_num>")
            else:
                try:
                    message_num = int(parts[1])
                    cmd_list_reactions(session, message_num)
                except ValueError:
                    print("error: message number must be an integer")

        elif cmd == "time":
            cmd_time(session)

        elif cmd == "net":
            cmd_network_show(session)

        elif cmd == "net-set":
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--latency", type=int)
            parser.add_argument("--jitter", type=int)
            parser.add_argument("--loss", type=float)
            parser.add_argument("--burst-prob", type=float)
            parser.add_argument("--burst-len", type=int)
            try:
                args = parser.parse_args(parts[1:])
                cmd_network_set(session, latency=args.latency, jitter=args.jitter,
                               loss=args.loss, burst_prob=args.burst_prob, burst_len=args.burst_len)
            except SystemExit:
                print("usage: net-set --latency <ms> --jitter <ms> --loss <percent> --burst-prob <percent> --burst-len <n>")

        elif cmd == "net-preset":
            if len(parts) < 2:
                print("usage: net-preset <preset>")
                print("  presets: lan, broadband, mobile-4g, mobile-3g, satellite, lossy")
            else:
                cmd_network_preset(session, parts[1])

        elif cmd == "partition":
            if len(parts) < 2:
                print("usage: partition <account_num>")
            else:
                try:
                    cmd_partition(session, int(parts[1]))
                except ValueError:
                    print("error: account number must be an integer")

        elif cmd == "unpartition":
            if len(parts) < 2:
                print("usage: unpartition <account_num>")
            else:
                try:
                    cmd_unpartition(session, int(parts[1]))
                except ValueError:
                    print("error: account number must be an integer")

        elif cmd == "log":
            cmd_toggle_log(session)

        elif cmd == "disappear":
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--days", type=int)
            parser.add_argument("--time", type=int)
            parser.add_argument("--off", action="store_true")
            try:
                args = parser.parse_args(parts[1:])
                if not args.days and not args.time and not args.off:
                    print("usage: disappear --days <n> | --time <ms> | --off")
                else:
                    cmd_set_disappearing(session, days=args.days, time_ms=args.time, off=args.off)
            except SystemExit:
                print("usage: disappear --days <n> | --time <ms> | --off")

        elif cmd == "fast-forward":
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--days", type=int, required=True)
            try:
                args = parser.parse_args(parts[1:])
                cmd_fast_forward(session, args.days)
            except SystemExit:
                print("usage: fast-forward --days <n>")

        elif cmd == "help":
            print("available commands:")
            print()
            print("  Network setup:")
            print("    new-network --name <name> --username <username> --devicename <device>")
            print()
            print("  Joining/linking:")
            print("    invite                         Create invite for new user to join network")
            print("    accept-invite --username <name> --devicename <device> --invite <n|link>")
            print("                                   Join network as NEW user")
            print("    link                           Create device link invite for current user")
            print("    accept-link --devicename <device> --invite <n|link>")
            print("                                   Link device to EXISTING user (no username needed)")
            print()
            print("  Account management:")
            print("    switch <n>")
            print("    accounts")
            print("    users")
            print()
            print("  Channels:")
            print("    channel <n>")
            print("    new-channel <name>")
            print("    channels")
            print()
            print("  Messaging:")
            print("    send <message>")
            print("    messages")
            print("    delete <n>")
            print("    edit <n> <new_content>")
            print("    react <n> <emoji>")
            print("    unreact <n> <emoji>")
            print("    reactions <n>")
            print("    disappear --days <n> | --time <ms> | --off")
            print()
            print("  Admin:")
            print("    ban <n>")
            print()
            print("  Keys/sync:")
            print("    keys [--summary]")
            print("    purge-keys")
            print("    tick <n>")
            print("    sync --ticks <n>")
            print("    auto-tick <n>")
            print()
            print("  Network simulation:")
            print("    net                            Show network simulation config")
            print("    net-set --latency <ms> --jitter <ms> --loss <percent>")
            print("            --burst-prob <percent> --burst-len <n>")
            print("    net-preset <preset>            Apply preset (lan, broadband, mobile-4g,")
            print("                                   mobile-3g, satellite, lossy)")
            print("    partition <n>                  Block network traffic for account #n")
            print("    unpartition <n>                Restore network traffic for account #n")
            print()
            print("  Other:")
            print("    status")
            print("    time")
            print("    log                            Toggle event log display ON/OFF")
            print("    fast-forward --days <n>")
            print("    quit")

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
