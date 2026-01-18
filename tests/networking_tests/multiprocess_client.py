"""
Multiprocess client for real networking tests.

Each client runs in a separate process with its own:
- Transport module state
- SQLite database
- UDP socket

Communication with the test is via multiprocessing Queues.
"""
import os
import sqlite3
import logging
from multiprocessing import Process, Queue
from typing import Any, Optional
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


def client_worker(cmd_queue: Queue, result_queue: Queue, db_path: str, udp_port: int, name: str):
    """Worker function that runs in a separate process.

    Has its own fresh imports, so no shared state with other processes.
    """
    # Configure logging for this process
    logging.basicConfig(
        level=logging.WARNING,
        format=f'[{name}] %(message)s'
    )

    # Fresh imports - completely isolated from other processes
    from core import transport, schema, tick as tick_module
    from core.db import Database
    from events.identity import user, invite, peer
    from events.content import message, channel
    from events.network import connection as conn_module

    # Initialize database
    conn = sqlite3.connect(db_path)
    db = Database(conn)
    schema.create_all(db)
    db.commit()

    # Start UDP
    transport.start_udp('127.0.0.1', udp_port)
    log.warning(f"Started UDP on port {udp_port}")

    # State
    state = {
        'peer_id': None,
        'peer_shared_id': None,
        'user_id': None,
        'network_id': None,
        'channel_id': None,
    }

    try:
        while True:
            cmd = cmd_queue.get()
            action = cmd.get('action')

            try:
                if action == 'new_network':
                    result = user.new_network(
                        name=cmd['name'],
                        t_ms=cmd['t_ms'],
                        db=db
                    )
                    db.commit()
                    state.update({
                        'peer_id': result['peer_id'],
                        'peer_shared_id': result['peer_shared_id'],
                        'user_id': result['user_id'],
                        'network_id': result['network_id'],
                        'channel_id': result['channel_id'],
                    })
                    result_queue.put({'ok': True, 'result': result, 'state': state.copy()})

                elif action == 'create_invite':
                    invite_id, invite_link, invite_data = invite.create(
                        peer_id=state['peer_id'],
                        t_ms=cmd['t_ms'],
                        db=db
                    )
                    db.commit()
                    result_queue.put({'ok': True, 'invite_id': invite_id, 'invite_link': invite_link})

                elif action == 'create_peer':
                    peer_id = peer.create(t_ms=cmd['t_ms'], db=db)
                    db.commit()
                    state['peer_id'] = peer_id
                    result_queue.put({'ok': True, 'peer_id': peer_id})

                elif action == 'join':
                    result = user.join(
                        peer_id=state['peer_id'],
                        invite_link=cmd['invite_link'],
                        name=cmd['name'],
                        t_ms=cmd['t_ms'],
                        db=db
                    )
                    db.commit()
                    state.update({
                        'peer_id': result['peer_id'],
                        'peer_shared_id': result['peer_shared_id'],
                        'user_id': result['user_id'],
                        'network_id': result['network_id'],
                    })
                    result_queue.put({'ok': True, 'result': result, 'state': state.copy()})

                elif action == 'add_peer_address':
                    transport.add_peer_address(cmd['peer_shared_id'], cmd['host'], cmd['port'])
                    result_queue.put({'ok': True})

                elif action == 'tick':
                    tick_module.tick(t_ms=cmd['t_ms'], db=db)
                    db.commit()
                    # Debug: report packet counts
                    incoming, outgoing = transport.pending_count()
                    result_queue.put({'ok': True, 'incoming': incoming, 'outgoing': outgoing})

                elif action == 'send_message':
                    msg_id = message.create(
                        content=cmd['content'],
                        channel_id=cmd.get('channel_id') or state['channel_id'],
                        peer_id=state['peer_id'],
                        t_ms=cmd['t_ms'],
                        db=db
                    )
                    db.commit()
                    result_queue.put({'ok': True, 'message_id': msg_id})

                elif action == 'get_messages':
                    channel_id = cmd.get('channel_id') or state['channel_id']
                    if channel_id:
                        msgs = message.list(channel_id, state['peer_id'], db)
                    else:
                        msgs = []
                    result_queue.put({'ok': True, 'messages': msgs})

                elif action == 'get_connections':
                    conns = conn_module.get_connections(
                        peer_id=state['peer_id'],
                        t_ms=cmd['t_ms'],
                        db=db
                    )
                    # Serialize connections
                    conn_data = [{
                        'connection_id': c.connection_id,
                        'peer_shared_id': c.peer_shared_id,
                        'can_send': c.can_send(),
                        'is_pending': c.is_pending(),
                    } for c in conns]
                    result_queue.put({'ok': True, 'connections': conn_data})

                elif action == 'get_users':
                    from events.identity import network as network_module
                    from events.group import group_member
                    if state['network_id']:
                        all_users_group_id = network_module.get_all_users_group_id(
                            state['network_id'], state['peer_id'], db
                        )
                        users = group_member.list_members(all_users_group_id, state['peer_id'], db)
                    else:
                        users = []
                    result_queue.put({'ok': True, 'users': users})

                elif action == 'get_channels':
                    channels = channel.list(state['peer_id'], db)
                    result_queue.put({'ok': True, 'channels': channels})

                elif action == 'get_state':
                    result_queue.put({'ok': True, 'state': state.copy()})

                elif action == 'get_peer_addresses':
                    # Debug: return registered peer addresses
                    addrs = dict(transport._peer_addresses)
                    result_queue.put({'ok': True, 'addresses': addrs})

                elif action == 'get_connection_addrs':
                    # Debug: return connection addresses from DB
                    from core.db import create_safe_db
                    sdb = create_safe_db(db, recorded_by=state['peer_id'])
                    conns = sdb.query("""
                        SELECT connection_id, peer_shared_id, from_addr_ip, from_addr_port
                        FROM connections WHERE recorded_by = ?
                    """, (state['peer_id'],))
                    result_queue.put({'ok': True, 'connections': [dict(c) for c in conns]})

                elif action == 'stop':
                    result_queue.put({'ok': True})
                    break

                else:
                    result_queue.put({'ok': False, 'error': f'Unknown action: {action}'})

            except Exception as e:
                log.exception(f"Error handling {action}")
                result_queue.put({'ok': False, 'error': str(e)})

    finally:
        transport.stop_udp()
        conn.close()
        log.warning("Worker stopped")


@dataclass
class RemoteClient:
    """Proxy to a client running in a separate process.

    Provides a clean API for sending commands and getting results.
    """
    name: str
    db_path: str
    udp_port: int

    _process: Optional[Process] = field(default=None, repr=False)
    _cmd_queue: Optional[Queue] = field(default=None, repr=False)
    _result_queue: Optional[Queue] = field(default=None, repr=False)

    # Cached state from last operation
    peer_id: Optional[str] = None
    peer_shared_id: Optional[str] = None
    user_id: Optional[str] = None
    network_id: Optional[str] = None
    channel_id: Optional[str] = None

    def start(self):
        """Start the client process."""
        # Clean up old db file if exists
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

        self._cmd_queue = Queue()
        self._result_queue = Queue()
        self._process = Process(
            target=client_worker,
            args=(self._cmd_queue, self._result_queue, self.db_path, self.udp_port, self.name)
        )
        self._process.start()

    def stop(self):
        """Stop the client process."""
        if self._process and self._process.is_alive():
            self._cmd_queue.put({'action': 'stop'})
            self._result_queue.get(timeout=5)  # Wait for ack
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.terminate()

    def _send(self, cmd: dict, expect_result: bool = True) -> Optional[dict]:
        """Send command and optionally wait for result."""
        self._cmd_queue.put(cmd)
        if expect_result:
            result = self._result_queue.get(timeout=30)
            if not result.get('ok'):
                raise Exception(f"Command failed: {result.get('error')}")
            return result
        return None

    def _update_state(self, state: dict):
        """Update cached state from result."""
        if state:
            self.peer_id = state.get('peer_id')
            self.peer_shared_id = state.get('peer_shared_id')
            self.user_id = state.get('user_id')
            self.network_id = state.get('network_id')
            self.channel_id = state.get('channel_id')

    # ========================================================================
    # High-level API
    # ========================================================================

    def new_network(self, name: str, t_ms: int) -> dict:
        """Create a new network."""
        result = self._send({'action': 'new_network', 'name': name, 't_ms': t_ms})
        self._update_state(result.get('state'))
        return result['result']

    def create_invite(self, t_ms: int) -> str:
        """Create an invite and return the link."""
        result = self._send({'action': 'create_invite', 't_ms': t_ms})
        return result['invite_link']

    def create_peer(self, t_ms: int) -> str:
        """Create a peer and return the peer_id."""
        result = self._send({'action': 'create_peer', 't_ms': t_ms})
        return result['peer_id']

    def join(self, invite_link: str, name: str, t_ms: int) -> dict:
        """Join a network using an invite link."""
        result = self._send({
            'action': 'join',
            'invite_link': invite_link,
            'name': name,
            't_ms': t_ms
        })
        self._update_state(result.get('state'))
        return result['result']

    def add_peer_address(self, peer_shared_id: str, host: str, port: int):
        """Register a peer's network address."""
        self._send({'action': 'add_peer_address', 'peer_shared_id': peer_shared_id, 'host': host, 'port': port})

    def tick(self, t_ms: int) -> dict:
        """Run a tick and wait for completion. Returns packet counts."""
        result = self._send({'action': 'tick', 't_ms': t_ms})
        return {'incoming': result.get('incoming', 0), 'outgoing': result.get('outgoing', 0)}

    def send_message(self, content: str, t_ms: int, channel_id: str = None) -> str:
        """Send a message and return the message_id."""
        result = self._send({
            'action': 'send_message',
            'content': content,
            't_ms': t_ms,
            'channel_id': channel_id
        })
        return result['message_id']

    def get_messages(self, limit: int = 100, channel_id: str = None) -> list:
        """Get messages from a channel."""
        result = self._send({
            'action': 'get_messages',
            'limit': limit,
            'channel_id': channel_id
        })
        return result['messages']

    def get_connections(self, t_ms: int) -> list:
        """Get connection status."""
        result = self._send({'action': 'get_connections', 't_ms': t_ms})
        return result['connections']

    def get_users(self) -> list:
        """Get users in the network."""
        result = self._send({'action': 'get_users'})
        return result['users']

    def get_channels(self) -> list:
        """Get channels in the network."""
        result = self._send({'action': 'get_channels'})
        return result['channels']

    def get_state(self) -> dict:
        """Get current state."""
        result = self._send({'action': 'get_state'})
        self._update_state(result.get('state'))
        return result['state']

    def get_peer_addresses(self) -> dict:
        """Debug: Get registered peer addresses."""
        result = self._send({'action': 'get_peer_addresses'})
        return result['addresses']


def tick_all(*clients: RemoteClient, t_ms: int, rounds: int = 1, interval_ms: int = 100):
    """Tick all clients for multiple rounds.

    Since each client is in its own process with real UDP,
    we tick all clients then give UDP time to deliver.
    """
    import time

    for i in range(rounds):
        current_t = t_ms + i * interval_ms
        for client in clients:
            client.tick(current_t)
        # Give UDP time to deliver between rounds
        time.sleep(0.02)
