#!/usr/bin/env python3
"""Tests for invite flows."""
import sys
import base64
import json
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0, '/home/hwilson/quiet-python-poc-5')

from protocols.quiet.events.invite.flows import create
from core.flows import FlowCtx


class TestInviteCreate:
    """Test invite.create flow."""

    def setup_method(self):
        """Set up test environment."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / 'test.db'
        self.db = sqlite3.connect(self.db_path)

        # Mock runner for event emission
        self.mock_runner = MagicMock()
        self.emitted_events = []

        def mock_run(**kwargs):
            """Capture emitted events and return IDs."""
            envelopes = kwargs.get('input_envelopes', [])
            self.emitted_events.extend(envelopes)
            # Return mock IDs for emitted events
            result = {}
            for env in envelopes:
                event_type = env.get('event_type')
                if event_type:
                    # Generate deterministic ID for testing
                    result[event_type] = f"mock_{event_type}_id"
            return result

        self.mock_runner.run.side_effect = mock_run

    def create_test_params(self, **overrides):
        """Create test parameters with required FlowCtx fields."""
        params = {
            'network_id': 'test_network',
            'group_id': 'test_group',
            'peer_id': 'test_peer',
            'peer_secret_id': 'test_peer_secret',
            '_db': self.db,
            '_runner': self.mock_runner,
            '_protocol_dir': 'protocols/quiet',
            '_request_id': 'test_request',
        }
        params.update(overrides)
        return params

    def test_create_invite_success(self):
        """Test successful invite creation."""
        params = self.create_test_params()

        # Mock query responses
        with patch.object(FlowCtx, 'query') as mock_query:
            # Mock prekey query
            mock_query.side_effect = [
                {  # prekey.get_active_for_peer response
                    'prekey_id': 'test_prekey_id',
                    'public_key': 'test_prekey_public'
                },
                {  # address.get_latest_for_peer response
                    'ip': '192.168.1.100',
                    'port': 9090
                }
            ]

            result = create(params)

        # Check result structure
        assert 'ids' in result
        assert 'invite' in result['ids']
        assert result['ids']['invite'] == 'mock_invite_id'

        assert 'data' in result
        assert 'invite_link' in result['data']
        assert 'invite_code' in result['data']
        assert result['data']['network_id'] == 'test_network'
        assert result['data']['group_id'] == 'test_group'

        # Verify invite link format
        link = result['data']['invite_link']
        assert link.startswith('quiet://invite/')

        # Decode and verify invite data
        code = link[15:]  # Strip 'quiet://invite/'
        padded = code + '=' * (-len(code) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        invite_data = json.loads(decoded)

        assert 'invite_secret' in invite_data
        assert invite_data['network_id'] == 'test_network'
        assert invite_data['group_id'] == 'test_group'
        assert invite_data['ip'] == '192.168.1.100'
        assert invite_data['port'] == 9090
        assert 'invite_key_secret' in invite_data
        assert 'transit_secret' in invite_data
        assert 'transit_secret_id' in invite_data

        # Check emitted events
        assert len(self.emitted_events) == 2  # key_secret and invite

        # Verify key_secret event
        key_event = next(e for e in self.emitted_events if e['event_type'] == 'key_secret')
        assert key_event['local_only'] is True
        assert key_event['self_created'] is True
        assert 'unsealed_secret' in key_event['event_plaintext']

        # Verify invite event
        invite_event = next(e for e in self.emitted_events if e['event_type'] == 'invite')
        assert invite_event['peer_id'] == 'test_peer'
        assert invite_event['event_plaintext']['network_id'] == 'test_network'
        assert invite_event['event_plaintext']['group_id'] == 'test_group'
        assert 'invite_pubkey' in invite_event['event_plaintext']
        assert 'invite_key_secret_id' in invite_event['event_plaintext']
        assert 'transit_secret_id' in invite_event['event_plaintext']

    def test_create_invite_without_peer_id(self):
        """Test that invite creation fails without peer_id."""
        params = self.create_test_params(peer_id='')

        try:
            create(params)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert 'peer_id is required' in str(e)

    def test_create_invite_without_prekey(self):
        """Test invite creation when no prekey is available."""
        params = self.create_test_params()

        # Mock query to return no prekey
        with patch.object(FlowCtx, 'query') as mock_query:
            mock_query.side_effect = [
                Exception("No prekey found"),  # prekey query fails
                {'ip': '127.0.0.1', 'port': 8080}  # address query succeeds
            ]

            result = create(params)

        # Should still succeed but without prekey info
        assert 'invite_link' in result['data']

        # Decode invite and check prekey fields are absent
        code = result['data']['invite_link'][15:]
        padded = code + '=' * (-len(code) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        invite_data = json.loads(decoded)

        assert 'prekey_secret_id' not in invite_data
        assert 'prekey_public' not in invite_data

        # But other required fields should be present
        assert 'invite_secret' in invite_data
        assert 'network_id' in invite_data
        assert 'group_id' in invite_data

    def test_create_invite_with_default_address(self):
        """Test invite creation with default address when query fails."""
        params = self.create_test_params()

        # Mock queries to fail
        with patch.object(FlowCtx, 'query') as mock_query:
            mock_query.side_effect = Exception("Query failed")

            result = create(params)

        # Decode invite and check default address
        code = result['data']['invite_link'][15:]
        padded = code + '=' * (-len(code) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        invite_data = json.loads(decoded)

        assert invite_data['ip'] == '127.0.0.1'
        assert invite_data['port'] == 8080

    def test_invite_link_is_urlsafe(self):
        """Test that invite link uses URL-safe base64 encoding."""
        params = self.create_test_params()

        with patch.object(FlowCtx, 'query') as mock_query:
            mock_query.side_effect = Exception("Query failed")

            result = create(params)

        code = result['data']['invite_code']

        # URL-safe base64 should not contain +, /, or =
        assert '+' not in code
        assert '/' not in code
        assert '=' not in code  # Padding is stripped

        # Should be decodable with urlsafe_b64decode
        padded = code + '=' * (-len(code) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        json.loads(decoded)  # Should parse as JSON

    def test_invite_includes_peer_secret_dep(self):
        """Test that invite event includes peer_secret_id in deps when provided."""
        params = self.create_test_params(peer_secret_id='test_peer_secret')

        with patch.object(FlowCtx, 'query') as mock_query:
            mock_query.side_effect = Exception("Query failed")

            create(params)

        # Check invite event deps
        invite_event = next(e for e in self.emitted_events if e['event_type'] == 'invite')
        assert 'test_group' in invite_event['deps']
        assert 'test_peer_secret' in invite_event['deps']

    def test_invite_data_keys_are_sorted(self):
        """Test that invite JSON has sorted keys for consistency."""
        params = self.create_test_params()

        with patch.object(FlowCtx, 'query') as mock_query:
            mock_query.side_effect = Exception("Query failed")

            result = create(params)

        # Decode and re-encode with sorted keys
        code = result['data']['invite_link'][15:]
        padded = code + '=' * (-len(code) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()

        # The JSON should already have sorted keys
        invite_data = json.loads(decoded)
        resorted = json.dumps(invite_data, separators=(',', ':'), sort_keys=True)

        assert decoded == resorted

    def test_invite_contains_all_required_fields(self):
        """Test that invite contains all fields required for join."""
        params = self.create_test_params()

        with patch.object(FlowCtx, 'query') as mock_query:
            mock_query.side_effect = [
                {'prekey_id': 'pk123', 'public_key': 'pubkey'},
                {'ip': '10.0.0.1', 'port': 7777}
            ]

            result = create(params)

        # These are the fields that join_as_user expects
        code = result['data']['invite_link'][15:]
        padded = code + '=' * (-len(code) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        invite_data = json.loads(decoded)

        required_fields = [
            'invite_secret',
            'network_id',
            'group_id',
            'invite_key_secret',
            'transit_secret',
            'transit_secret_id',
            'ip',
            'port'
        ]

        for field in required_fields:
            assert field in invite_data, f"Missing required field: {field}"

    def test_secrets_are_hex_encoded(self):
        """Test that secrets in invite data are hex-encoded strings."""
        params = self.create_test_params()

        with patch.object(FlowCtx, 'query') as mock_query:
            mock_query.side_effect = Exception("Query failed")

            result = create(params)

        code = result['data']['invite_link'][15:]
        padded = code + '=' * (-len(code) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        invite_data = json.loads(decoded)

        # Check that secrets are hex strings
        invite_key_secret = invite_data['invite_key_secret']
        transit_secret = invite_data['transit_secret']

        # Should be hex strings (even length, all hex chars)
        assert len(invite_key_secret) == 64  # 32 bytes as hex
        assert all(c in '0123456789abcdef' for c in invite_key_secret)

        assert len(transit_secret) == 64  # 32 bytes as hex
        assert all(c in '0123456789abcdef' for c in transit_secret)