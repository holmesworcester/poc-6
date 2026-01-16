"""
Scenario tests for CLI file demo commands.

Tests:
1. send-with-image creates message with 200KB attachment
2. Inline attachment display shows progress percentage
3. files command lists attachments with progress
4. pause/resume commands work correctly
5. Two-party sync delivers file to recipient
"""
import pytest
from cli import (
    CLISession, cmd_send_with_image, cmd_files, cmd_pause_file,
    cmd_resume_file, display_main, _format_size_short, _format_progress_bar
)
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import sync_file
from core import tick


@pytest.fixture
def cli_session(fresh_db):
    """Create a CLI session with initialized database."""
    session = CLISession()
    session.db = fresh_db
    session.auto_tick_count = 0  # Manual control
    return session


def test_format_size_short():
    """Test size formatting helper."""
    assert _format_size_short(500) == "500B"
    assert _format_size_short(1024) == "1KB"
    assert _format_size_short(200 * 1024) == "200KB"
    assert _format_size_short(1024 * 1024) == "1.0MB"
    assert _format_size_short(1024 * 1024 * 1024) == "1.0GB"
    assert _format_size_short(int(1.5 * 1024 * 1024)) == "1.5MB"


def test_format_progress_bar():
    """Test progress bar formatting."""
    assert _format_progress_bar(0) == "[░░░░░░░░░░]"
    assert _format_progress_bar(50) == "[█████░░░░░]"
    assert _format_progress_bar(100) == "[██████████]"
    assert _format_progress_bar(33) == "[███░░░░░░░]"


def test_send_with_image_creates_attachment(cli_session):
    """Test that send-with-image creates a message with 200KB attachment."""
    session = cli_session

    # Create network
    result = user.new_network(name='alice', t_ms=1000, db=session.db)
    session.db.commit()

    # Setup session state
    from cli import AccountContext
    account = AccountContext(
        user_name='alice',
        device_name='desktop',
        peer_id=result['peer_id'],
        peer_shared_id=result['peer_shared_id']
    )
    account.user_id = result['user_id']
    account.network_id = result['network_id']
    session.accounts[account.full_name] = account
    session.selected_account = account.full_name
    session.selected_channel_id = result['channel_id']
    session.current_time_ms = 2000

    # Send with image
    cmd_send_with_image(session, "Test message")
    session.db.commit()

    # Verify message was created
    messages = message.list(result['channel_id'], result['peer_id'], session.db)
    assert len(messages) == 1
    assert messages[0]['content'] == "Test message"

    # Verify attachment
    attachments = messages[0].get('attachments', [])
    assert len(attachments) == 1
    att = attachments[0]
    assert att['mime_type'] == 'image/png'
    assert att['blob_bytes'] == 200 * 1024  # 200KB
    assert att['filename'].startswith('demo-image-')

    # Verify file is complete for sender
    progress = message_attachment.get_file_download_progress(
        att['file_id'], result['peer_id'], session.db
    )
    assert progress is not None
    assert progress['is_complete'] == True
    assert progress['percentage_complete'] == 100


def test_files_command_lists_attachments(cli_session):
    """Test that files command lists attachments with progress."""
    session = cli_session

    # Create network and send image
    result = user.new_network(name='alice', t_ms=1000, db=session.db)
    session.db.commit()

    # Setup session
    from cli import AccountContext
    account = AccountContext(
        user_name='alice',
        device_name='desktop',
        peer_id=result['peer_id'],
        peer_shared_id=result['peer_shared_id']
    )
    account.user_id = result['user_id']
    account.network_id = result['network_id']
    session.accounts[account.full_name] = account
    session.selected_account = account.full_name
    session.selected_channel_id = result['channel_id']
    session.current_time_ms = 2000

    # Send with image
    cmd_send_with_image(session, "Test")
    session.db.commit()

    # Run files command
    cmd_files(session)

    # Verify file_list was populated
    assert hasattr(session, 'file_list')
    assert len(session.file_list) == 1
    assert session.file_list[0]['blob_bytes'] == 200 * 1024
    assert session.file_list[0]['progress']['is_complete'] == True


def test_two_party_file_sync_with_progress(cli_session):
    """Test file sync between two parties with progress tracking."""
    from tests.utils.tick_helper import assert_eventually
    session = cli_session

    # Alice creates network
    alice = user.new_network(name='alice', t_ms=1000, db=session.db)
    session.db.commit()

    # Alice creates invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'], t_ms=1500, db=session.db
    )
    session.db.commit()

    # Bob joins
    bob_peer_id = peer.create(t_ms=2000, db=session.db)
    bob = user.join(
        peer_id=bob_peer_id,
        invite_link=invite_link,
        name='bob',
        t_ms=2000,
        db=session.db
    )
    session.db.commit()

    # Alice sends message with attachment
    import os
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this image!',
        t_ms=3000,
        db=session.db
    )

    file_data = os.urandom(10 * 1024)  # 10KB - enough to test sync
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test-image.png',
        mime_type='image/png',
        t_ms=3100,
        db=session.db
    )
    file_id = file_result['file_id']
    session.db.commit()

    # Verify Alice has complete file
    alice_progress = message_attachment.get_file_download_progress(
        file_id, alice['peer_id'], session.db
    )
    assert alice_progress['is_complete'] == True

    # Wait for Bob to receive complete file
    def bob_has_complete_file():
        progress = message_attachment.get_file_download_progress(
            file_id, bob['peer_id'], session.db
        )
        assert progress is not None, "Bob should know about the file"
        assert progress['is_complete'], f"Bob should have complete file, got {progress['percentage_complete']}%"

    assert_eventually(bob_has_complete_file, db=session.db, start_t_ms=4000)

    # Verify Bob can retrieve the file
    bob_file_data = message_attachment.get_file_data(file_id, bob['peer_id'], session.db)
    assert bob_file_data == file_data, "Bob's file should match Alice's"

    print(f"\n✓ File sync complete")


def test_pause_resume_file_download(cli_session):
    """Test pause and resume functionality."""
    session = cli_session

    # Create network
    result = user.new_network(name='alice', t_ms=1000, db=session.db)
    session.db.commit()

    # Setup session
    from cli import AccountContext
    account = AccountContext(
        user_name='alice',
        device_name='desktop',
        peer_id=result['peer_id'],
        peer_shared_id=result['peer_shared_id']
    )
    account.user_id = result['user_id']
    account.network_id = result['network_id']
    session.accounts[account.full_name] = account
    session.selected_account = account.full_name
    session.selected_channel_id = result['channel_id']
    session.current_time_ms = 2000

    # Send with image to create a file
    cmd_send_with_image(session, "Test")
    session.db.commit()

    # Populate file list
    cmd_files(session)
    assert len(session.file_list) == 1

    file_id = session.file_list[0]['file_id']

    # Test pause (even though file is complete, API should not error)
    cmd_pause_file(session, 1)
    session.db.commit()

    # Test resume
    cmd_resume_file(session, 1)
    session.db.commit()

    # Verify file is still accessible
    progress = message_attachment.get_file_download_progress(
        file_id, result['peer_id'], session.db
    )
    assert progress['is_complete'] == True


def test_inline_attachment_display(cli_session, capsys):
    """Test that messages display inline attachment info."""
    session = cli_session

    # Create network
    result = user.new_network(name='alice', t_ms=1000, db=session.db)
    session.db.commit()

    # Setup session
    from cli import AccountContext
    account = AccountContext(
        user_name='alice',
        device_name='desktop',
        peer_id=result['peer_id'],
        peer_shared_id=result['peer_shared_id']
    )
    account.user_id = result['user_id']
    account.network_id = result['network_id']
    session.accounts[account.full_name] = account
    session.selected_account = account.full_name
    session.selected_channel_id = result['channel_id']
    session.current_time_ms = 2000

    # Send with image
    cmd_send_with_image(session, "Photo here")
    session.db.commit()

    # Display main (captures output)
    display_main(session)

    captured = capsys.readouterr()

    # Verify inline attachment display
    assert "[img 200KB 100%]" in captured.out, f"Expected inline display, got: {captured.out}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
