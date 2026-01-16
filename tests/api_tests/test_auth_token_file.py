"""
Tests for PSK token file I/O.

These tests verify that:
- Token is generated and written to the correct path
- Token file has correct permissions (0600)
- Token can be read back from file
- Token is 32 bytes (crypto.generate_secret() size)
"""

import os
import stat
import pytest
from pathlib import Path

from api.core import auth as api_auth_module


@pytest.fixture
def token_dir(tmp_path):
    """Create a temporary directory for token files."""
    return tmp_path / "quiet_test"


def test_init_auth_writes_token_to_file(token_dir):
    """init_auth() writes token to the specified directory."""
    peer_id = "test-peer-123"

    # Initialize auth with custom directory
    token = api_auth_module.init_auth(peer_id=peer_id, token_dir=token_dir)

    # Token should be 32 bytes
    assert len(token) == 32
    assert isinstance(token, bytes)

    # File should exist
    token_path = token_dir / "api_token"
    assert token_path.exists()

    # File contents should match
    file_contents = token_path.read_bytes()
    assert file_contents == token

    print("init_auth writes token to file")


def test_token_file_has_restrictive_permissions(token_dir):
    """Token file should have 0600 permissions (owner read/write only)."""
    peer_id = "test-peer-456"

    api_auth_module.init_auth(peer_id=peer_id, token_dir=token_dir)

    token_path = token_dir / "api_token"
    file_stat = os.stat(token_path)
    permissions = stat.S_IMODE(file_stat.st_mode)

    # 0o600 = owner read + write only
    assert permissions == 0o600, f"Expected 0600, got {oct(permissions)}"

    print("Token file has restrictive permissions (0600)")


def test_read_token_from_file(token_dir):
    """read_token_from_file() returns the token written by init_auth()."""
    peer_id = "test-peer-789"

    # Write token
    written_token = api_auth_module.init_auth(peer_id=peer_id, token_dir=token_dir)

    # Read it back
    read_token = api_auth_module.read_token_from_file(peer_id, token_dir=token_dir)

    assert read_token == written_token
    assert len(read_token) == 32

    print("read_token_from_file returns correct token")


def test_read_token_returns_none_for_missing_file(token_dir):
    """read_token_from_file() returns None if token file doesn't exist."""
    peer_id = "nonexistent-peer"

    result = api_auth_module.read_token_from_file(peer_id, token_dir=token_dir)

    assert result is None

    print("read_token_from_file returns None for missing file")


def test_token_is_cryptographically_random(token_dir):
    """Multiple calls to init_auth() should produce different tokens."""
    peer_id = "test-peer-random"

    # Generate multiple tokens
    tokens = []
    for i in range(5):
        # Use different subdirs to avoid overwriting
        subdir = token_dir / f"run{i}"
        token = api_auth_module.init_auth(peer_id=peer_id, token_dir=subdir)
        tokens.append(token)

    # All tokens should be unique
    unique_tokens = set(tokens)
    assert len(unique_tokens) == 5, "Tokens should be unique (cryptographically random)"

    print("Tokens are cryptographically random")


def test_init_auth_creates_parent_directories(tmp_path):
    """init_auth() creates parent directories if they don't exist."""
    peer_id = "nested-peer"
    deep_dir = tmp_path / "deep" / "nested" / "path"

    # Directory shouldn't exist yet
    assert not deep_dir.exists()

    api_auth_module.init_auth(peer_id=peer_id, token_dir=deep_dir)

    # Now it should exist with token file
    assert deep_dir.exists()
    assert (deep_dir / "api_token").exists()

    print("init_auth creates parent directories")


def test_init_auth_without_peer_id_does_not_write_file(token_dir):
    """init_auth() without peer_id generates token but doesn't write to disk."""
    # Clear any existing token
    api_auth_module.set_token(None)

    # Init without peer_id
    token = api_auth_module.init_auth(peer_id=None)

    # Token should be generated
    assert token is not None
    assert len(token) == 32

    # But no file should be written
    token_path = token_dir / "api_token"
    assert not token_path.exists()

    # Token should be set in memory
    assert api_auth_module.get_token() == token

    print("init_auth without peer_id doesn't write file")


def test_token_overwrites_on_restart(token_dir):
    """Restarting the API (calling init_auth again) generates a new token."""
    peer_id = "restart-test"

    # First "startup"
    token1 = api_auth_module.init_auth(peer_id=peer_id, token_dir=token_dir)

    # Second "startup" (simulates restart)
    token2 = api_auth_module.init_auth(peer_id=peer_id, token_dir=token_dir)

    # Tokens should be different
    assert token1 != token2

    # File should contain the new token
    file_token = api_auth_module.read_token_from_file(peer_id, token_dir=token_dir)
    assert file_token == token2

    print("Token is overwritten on restart")


def test_default_token_path():
    """get_token_path() returns the expected path structure."""
    peer_id = "my-peer-id"

    path = api_auth_module.get_token_path(peer_id)

    # Should be ~/.quiet/{peer_id}/api_token
    expected = Path.home() / ".quiet" / peer_id / "api_token"
    assert path == expected

    print("Default token path is correct")
