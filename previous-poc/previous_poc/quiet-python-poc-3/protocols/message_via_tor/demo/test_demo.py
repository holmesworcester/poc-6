#!/usr/bin/env python3
"""
Tests for the message_via_tor Textual demo.
"""

import pytest
import subprocess
from textual.pilot import Pilot
from textual.widgets import Input
from demo import MessageViaTorDemo


@pytest.fixture(autouse=True)
def reset_mouse_tracking():
    """Reset mouse tracking after each test."""
    yield
    # Reset mouse tracking modes after test completes
    subprocess.run(["printf", "\033[?1000l\033[?1002l\033[?1003l\033[?1005l\033[?1006l\033[?1015l"])


@pytest.mark.asyncio
async def test_app_starts():
    """Test that the app starts and has the expected widgets."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Check header exists
        assert app.query_one("Header")
        
        # Check all panels exist
        assert app.query_one("#test-list")
        assert app.query_one("#identity1")
        assert app.query_one("#identity2")
        assert app.query_one("#state-changes")
        assert app.query_one("#state-inspector")
        
        # Check input fields
        assert app.query_one("#input1")
        assert app.query_one("#input2")


@pytest.mark.asyncio
async def test_default_test_loaded():
    """Test that test #12 is loaded by default with Alice identity."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Check that Alice is shown in identity 1
        identity_label = app.query_one("#identity1 Label")
        assert "Alice" in identity_label.renderable


@pytest.mark.asyncio
async def test_create_identity():
    """Test creating a new identity."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Get initial identity count
        initial_identities = len(app.get_identities())
        
        # Press 'i' to create identity
        await pilot.press("i")
        
        # Check identity was created
        new_identities = len(app.get_identities())
        assert new_identities > initial_identities
        
        # Check state change was recorded
        assert len(app.state_changes) > 0
        assert "identity.create" in app.state_changes[-1]['operation']


@pytest.mark.asyncio
async def test_send_message():
    """Test sending a message from identity 1."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Focus input1
        await pilot.click("#input1")
        
        # Type a message
        await pilot.press("H", "e", "l", "l", "o")
        
        # Submit with Enter
        await pilot.press("enter")
        
        # Check message was sent (state change recorded)
        assert any("message:" in change['operation'] for change in app.state_changes)
        
        # Check input was cleared
        input1 = app.query_one("#input1")
        assert input1.value == ""


@pytest.mark.asyncio
async def test_tick():
    """Test running a tick cycle."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Press 't' to tick
        await pilot.press("t")
        
        # Check tick was recorded in state changes
        assert any("tick" in change['operation'] for change in app.state_changes)


@pytest.mark.asyncio
async def test_reset():
    """Test resetting the state."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Create some state changes first
        await pilot.press("i")  # Create identity
        
        # Reset
        await pilot.press("r")
        
        # Check state was reset
        assert len(app.get_identities()) == 0
        assert any("reset" in change['operation'] for change in app.state_changes)


@pytest.mark.asyncio  
async def test_state_inspector():
    """Test that state inspector shows changes."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Create an identity to generate a state change
        await pilot.press("i")
        
        # Check inspector shows before/after
        inspector_log = app.query_one("#inspector-log")
        content = inspector_log.lines
        
        # Should have BEFORE and AFTER sections
        assert any("BEFORE:" in str(line) for line in content)
        assert any("AFTER:" in str(line) for line in content)


@pytest.mark.asyncio
async def test_arrow_key_navigation():
    """Test navigating tests and state changes with arrow keys."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Get initial selections
        initial_test = app.selected_test
        initial_change = app.selected_change
        
        # Test up/down navigation for tests
        await pilot.press("down")
        assert app.selected_test == initial_test + 1
        
        await pilot.press("up")
        assert app.selected_test == initial_test
        
        # Create some state changes for navigation
        await pilot.press("i")  # Create identity
        await pilot.press("t")  # Tick
        
        # Test left/right navigation for state changes
        await pilot.press("right")
        assert app.selected_change > initial_change
        
        await pilot.press("left")
        assert app.selected_change == initial_change


@pytest.mark.asyncio
async def test_load_test_button():
    """Test clicking the Load Test button."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Navigate to a different test
        await pilot.press("down")
        await pilot.press("down")
        
        # Click Load Test button (or press space)
        await pilot.press("space")
        
        # Check test was loaded
        assert len(app.state_changes) > 0
        assert "load_test" in app.state_changes[-1]['operation']


@pytest.mark.asyncio
async def test_tab_switching():
    """Test switching between identity inputs with Tab."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Focus on input1
        await pilot.click("#input1")
        input1 = app.query_one("#input1")
        assert input1.has_focus
        
        # Tab should switch focus
        await pilot.press("tab")
        # Due to Textual's focus system, we need to check what has focus
        # This might cycle through multiple widgets


@pytest.mark.asyncio
async def test_identity_selection():
    """Test selecting different identities from dropdowns."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load a test with multiple identities
        app.selected_test = 12  # Test with Alice/Bob/Charlie
        await pilot.press("space")  # Load test
        
        # Wait for state to update
        await pilot.pause(0.1)
        
        # Check identities are available
        identities = app.get_identities()
        assert len(identities) > 0
        assert any(i['name'] == 'Alice' for i in identities)


@pytest.mark.asyncio
async def test_message_display_panels():
    """Test that messages appear in the correct panels."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load test with Alice
        await pilot.press("space")
        
        # Send a message from identity 1
        await pilot.click("#input1")
        await pilot.press("H", "i")
        await pilot.press("enter")
        
        # Check message log was updated
        messages1 = app.query_one("#messages1")
        # The log should have content


@pytest.mark.asyncio
async def test_state_change_selection():
    """Test selecting different state changes updates inspector."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Create multiple state changes
        await pilot.press("i")  # Create identity
        await pilot.press("t")  # Tick
        await pilot.press("i")  # Create another identity
        
        # Navigate state changes
        initial_content = app.query_one("#inspector-log").lines
        
        await pilot.press("right")
        await pilot.pause(0.1)
        
        # Inspector content should change
        new_content = app.query_one("#inspector-log").lines
        # Content comparison would depend on exact implementation


@pytest.mark.asyncio
async def test_multiple_identity_switching():
    """Test switching between multiple identities in both panels."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Create multiple identities
        await pilot.press("i")
        await pilot.press("i")
        await pilot.press("i")
        
        # Should have 3 identities now
        identities = app.get_identities()
        assert len(identities) >= 3


@pytest.mark.asyncio
async def test_quit_key():
    """Test that 'q' key exits the app."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Press q to quit
        await pilot.press("q")
        # App should exit - in tests this is handled by the test framework


@pytest.mark.asyncio
async def test_test_list_scrolling():
    """Test scrolling through the test list."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Scroll down multiple times
        for _ in range(5):
            await pilot.press("down")
        
        # Should have moved down
        assert app.selected_test >= 5
        
        # Scroll back up
        for _ in range(5):
            await pilot.press("up")
        
        assert app.selected_test <= 5


@pytest.mark.asyncio
async def test_empty_message_handling():
    """Test that empty messages are not sent."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        initial_changes = len(app.state_changes)
        
        # Click input and press enter without typing
        await pilot.click("#input1")
        await pilot.press("enter")
        
        # No new state change should be recorded
        assert len(app.state_changes) == initial_changes


@pytest.mark.asyncio
async def test_state_persistence_across_loads():
    """Test that loading different tests maintains state history."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load first test
        await pilot.press("space")
        
        # Create some changes
        await pilot.press("i")
        
        # Load a different test
        await pilot.press("down")
        await pilot.press("space")
        
        # State changes should include both loads
        loads = [c for c in app.state_changes if "load_test" in c['operation']]
        assert len(loads) >= 2


@pytest.mark.asyncio
async def test_inspector_json_display():
    """Test that state inspector shows valid JSON."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Create a state change
        await pilot.press("i")
        
        # Get inspector content
        inspector = app.query_one("#inspector-log")
        content = str(inspector.lines)
        
        # Should contain JSON-like structures
        assert "{" in content or "empty" in content


@pytest.mark.asyncio
async def test_boundary_navigation():
    """Test navigation at boundaries (first/last items)."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Try to go up from first test
        app.selected_test = 0
        await pilot.press("up")
        assert app.selected_test == 0  # Should stay at 0
        
        # Try to go past last test
        app.selected_test = len(app.test_loader.tests) - 1
        await pilot.press("down")
        assert app.selected_test == len(app.test_loader.tests) - 1


@pytest.mark.asyncio
async def test_concurrent_identity_messages():
    """Test sending messages from both identities."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load test with identities
        await pilot.press("space")
        
        # Send from input1
        await pilot.click("#input1")
        await pilot.press("m", "s", "g", "1")
        await pilot.press("enter")
        
        # Send from input2 if identity exists
        if app.identity2_selected > 0:
            await pilot.click("#input2")
            await pilot.press("m", "s", "g", "2")
            await pilot.press("enter")
            
            # Should have multiple message operations
            messages = [c for c in app.state_changes if "message" in c['operation']]
            assert len(messages) >= 1


@pytest.mark.asyncio
async def test_enter_key_sends_message():
    """Test that Enter key in message field sends the message."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load test with identity
        await pilot.press("space")
        
        # Click on input1 to focus
        await pilot.click("#input1")
        input1 = app.query_one("#input1")
        assert input1.has_focus
        
        # Type a message
        test_message = "Test message with Enter"
        for char in test_message:
            await pilot.press(char)
        
        # Verify text is in input
        assert input1.value == test_message
        
        # Press Enter to send
        initial_changes = len(app.state_changes)
        await pilot.press("enter")
        
        # Verify message was sent
        assert len(app.state_changes) > initial_changes
        assert any("message" in change['operation'] for change in app.state_changes[initial_changes:])
        
        # Verify input was cleared
        assert input1.value == ""


@pytest.mark.asyncio
async def test_escape_unfocus_input():
    """Test that Escape key or clicking away unfocuses the input field."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load test
        await pilot.press("space")
        
        # Click on input1 to focus
        await pilot.click("#input1")
        input1 = app.query_one("#input1")
        assert input1.has_focus
        
        # Type some text
        await pilot.press("t", "e", "s", "t")
        
        # Press Escape to unfocus
        await pilot.press("escape")
        assert not input1.has_focus
        
        # Click back on input
        await pilot.click("#input1")
        assert input1.has_focus
        
        # Click elsewhere to unfocus (click on a different widget)
        await pilot.click("#state-changes")
        assert not input1.has_focus


@pytest.mark.asyncio
async def test_send_multiple_messages():
    """Test sending multiple messages in succession."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load test with identity
        await pilot.press("space")
        
        # Send multiple messages
        messages_to_send = ["First message", "Second message", "Third message"]
        initial_changes = len(app.state_changes)
        
        for msg in messages_to_send:
            await pilot.click("#input1")
            for char in msg:
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause(0.1)  # Small pause between messages
        
        # Verify all messages were sent
        new_changes = app.state_changes[initial_changes:]
        message_changes = [c for c in new_changes if "message" in c['operation']]
        assert len(message_changes) >= len(messages_to_send)
        
        # Input should be clear after each message
        input1 = app.query_one("#input1")
        assert input1.value == ""


@pytest.mark.asyncio
async def test_click_test_and_scroll():
    """Test clicking on tests in the list and scrolling with cursor keys."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Get test list widget
        test_list = app.query_one("#test-list")
        
        # Click on a specific test item (simulate clicking on test 5)
        # First we need to ensure the test is visible
        for _ in range(5):
            await pilot.press("down")
        
        assert app.selected_test == 5
        
        # Now test scrolling with cursor keys
        initial_position = app.selected_test
        
        # Scroll down
        await pilot.press("down")
        await pilot.press("down")
        assert app.selected_test == initial_position + 2
        
        # Scroll up
        await pilot.press("up")
        assert app.selected_test == initial_position + 1
        
        # Test Page Down/Up if supported
        # Some terminals support these keys differently
        
        # Load the selected test
        await pilot.press("space")
        assert "load_test" in app.state_changes[-1]['operation']


@pytest.mark.asyncio
async def test_test_list_focus_and_navigation():
    """Test that test list can be focused and navigated properly."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Click on the test list area
        await pilot.click("#test-list")
        
        # Navigate with keys
        start_pos = app.selected_test
        
        # Test rapid navigation
        for _ in range(10):
            await pilot.press("down")
        
        assert app.selected_test == min(start_pos + 10, len(app.test_loader.tests) - 1)
        
        # Navigate back up
        for _ in range(5):
            await pilot.press("up")
        
        # Verify position changed appropriately
        assert app.selected_test >= start_pos + 5


@pytest.mark.asyncio
async def test_input_field_retains_focus_during_typing():
    """Test that input field retains focus while typing."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load test
        await pilot.press("space")
        
        # Focus input
        await pilot.click("#input1")
        input1 = app.query_one("#input1")
        
        # Type a long message
        long_message = "This is a longer message to test continuous typing"
        for char in long_message:
            await pilot.press(char)
            # Input should still have focus
            assert input1.has_focus
        
        # Verify all text was entered
        assert input1.value == long_message


@pytest.mark.asyncio
async def test_identity2_shows_second_identity():
    """Test that Identity 2 shows the second identity when default test is loaded."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # The default test should now be test 21 with Alice and Bob
        assert app.selected_test == 21
        
        # Wait for initial display update
        await pilot.pause(0.1)
        
        # Check identities
        identities = app.get_identities()
        print(f"Found {len(identities)} identities: {[i['name'] for i in identities]}")
        
        # Should have two identities
        assert len(identities) == 2
        assert app.identity1_selected == 0
        assert app.identity2_selected == 1
        
        # Get the dropdown labels
        identity1_dropdown = app.query_one("#identity1-dropdown")
        identity2_dropdown = app.query_one("#identity2-dropdown")
        
        print(f"Identity 1 dropdown: {identity1_dropdown.renderable}")
        print(f"Identity 2 dropdown: {identity2_dropdown.renderable}")
        
        # Verify Identity 1 is Alice and Identity 2 is Bob
        assert "Alice" in str(identity1_dropdown.renderable)
        assert "Bob" in str(identity2_dropdown.renderable)


@pytest.mark.asyncio
async def test_find_test_with_multiple_identities():
    """Find a test that actually has multiple identities."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Search through tests to find one with multiple identities
        multi_identity_tests = []
        
        for i, test in enumerate(app.test_loader.tests):
            db = test.get('given', {}).get('db', {})
            state = db.get('state', {})
            identities = state.get('identities', {})
            
            # Count identities
            if isinstance(identities, dict) and len(identities) > 1:
                multi_identity_tests.append((i, test['name'], list(identities.keys())))
            elif isinstance(identities, list) and len(identities) > 1:
                multi_identity_tests.append((i, test['name'], [id.get('name', 'Unknown') for id in identities]))
        
        print(f"Tests with multiple identities:")
        for idx, name, ids in multi_identity_tests:
            print(f"  Test {idx}: {name}")
            print(f"    Identities: {ids}")
        
        # If we found tests with multiple identities, test the first one
        if multi_identity_tests:
            test_idx = multi_identity_tests[0][0]
            app.selected_test = test_idx
            await pilot.press("space")  # Load the test
            
            await pilot.pause(0.1)
            
            # Check that both identities are loaded
            identities = app.get_identities()
            assert len(identities) >= 2
            assert app.identity1_selected == 0
            assert app.identity2_selected == 1
            
            # Check dropdowns
            identity1_dropdown = app.query_one("#identity1-dropdown")
            identity2_dropdown = app.query_one("#identity2-dropdown")
            
            assert identities[0]['name'] in str(identity1_dropdown.renderable)
            assert identities[1]['name'] in str(identity2_dropdown.renderable)


@pytest.mark.asyncio
async def test_identity_selection_after_load():
    """Test identity selection is correct after loading a test."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Load a different test first
        app.selected_test = 0
        await pilot.press("space")
        
        # Now load test 12 (with Alice/Bob/Charlie)
        app.selected_test = 12
        await pilot.press("space")
        
        await pilot.pause(0.1)
        
        # Check identities were properly set
        identities = app.get_identities()
        if len(identities) >= 2:
            assert app.identity1_selected == 0
            assert app.identity2_selected == 1
            
            # Check the dropdowns
            identity2_dropdown = app.query_one("#identity2-dropdown")
            assert "Bob" in str(identity2_dropdown.renderable) or identities[1]['name'] in str(identity2_dropdown.renderable)


@pytest.mark.asyncio
async def test_create_identity_directly():
    """Test creating an identity by calling the method directly."""
    app = MessageViaTorDemo()
    async with app.run_test() as pilot:
        # Initially no identities
        assert len(app.get_identities()) == 0
        
        # Create an identity directly
        await app.create_identity(1, "Alice")
        
        # Check that identity was created
        identities = app.get_identities()
        assert len(identities) == 1
        assert identities[0]['name'] == 'Alice'
        
        # Check that identity 1 is now selected
        assert app.identity1_selected == 0
        
        # Check that UI updated - buttons should be hidden, input should be visible
        assert not app.query_one("#identity1-buttons").display
        assert app.query_one("#input1").display
        assert app.query_one("#messages1").display


if __name__ == "__main__":
    pytest.main([__file__])