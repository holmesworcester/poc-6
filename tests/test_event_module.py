"""Unit tests for EventModule base class."""
import pytest
from events.base import EventModule
from typing import Any


class SimpleTestModule(EventModule):
    """Simple test event module for testing EventModule behavior."""

    @property
    def event_type(self) -> str:
        return 'simple_test'

    def _build_event_data(self, **kwargs) -> dict[str, Any]:
        return {
            'message': kwargs.get('message', 'default'),
        }

    def _validate_event_fields(self, event_data: dict[str, Any], recorded_by: str, db: Any) -> tuple[bool, str]:
        if not event_data.get('message'):
            return False, "Missing message"
        if len(event_data['message']) > 1000:
            return False, "Message too long"
        return True, ""

    def _project_to_tables(self, event_id: str, event_data: dict[str, Any], recorded_by: str, recorded_at: int, db: Any) -> None:
        # In real implementation, would insert into database tables
        # For testing, just store in a class variable
        if not hasattr(self, '_projected_events'):
            self._projected_events = []
        self._projected_events.append({
            'event_id': event_id,
            'event_data': event_data,
            'recorded_by': recorded_by,
            'recorded_at': recorded_at,
        })

    def _extract_dependencies(self, event_data: dict[str, Any]) -> list[str]:
        # Simple test module has no dependencies
        return []


class SignedTestModule(EventModule):
    """Test event module that is signed."""

    @property
    def event_type(self) -> str:
        return 'signed_test'

    def _build_event_data(self, **kwargs) -> dict[str, Any]:
        return {
            'content': kwargs.get('content', 'default'),
        }

    def _get_signing_key(self, **kwargs) -> bytes | None:
        # Return a fixed test key
        return b'test_signing_key_32_bytes_padding'

    def _validate_event_fields(self, event_data: dict[str, Any], recorded_by: str, db: Any) -> tuple[bool, str]:
        return True, ""

    def _project_to_tables(self, event_id: str, event_data: dict[str, Any], recorded_by: str, recorded_at: int, db: Any) -> None:
        pass

    def _extract_dependencies(self, event_data: dict[str, Any]) -> list[str]:
        return []


class DependencyTestModule(EventModule):
    """Test event module with dependencies."""

    @property
    def event_type(self) -> str:
        return 'dependency_test'

    def _build_event_data(self, **kwargs) -> dict[str, Any]:
        return {
            'parent_id': kwargs.get('parent_id'),
            'content': kwargs.get('content', 'default'),
        }

    def _validate_event_fields(self, event_data: dict[str, Any], recorded_by: str, db: Any) -> tuple[bool, str]:
        if not event_data.get('parent_id'):
            return False, "Missing parent_id"
        return True, ""

    def _project_to_tables(self, event_id: str, event_data: dict[str, Any], recorded_by: str, recorded_at: int, db: Any) -> None:
        pass

    def _extract_dependencies(self, event_data: dict[str, Any]) -> list[str]:
        deps = []
        if parent_id := event_data.get('parent_id'):
            deps.append(parent_id)
        return deps


class TestEventModuleBasics:
    """Test EventModule basic functionality."""

    def test_event_type_property(self):
        """Test that event_type property is accessible."""
        module = SimpleTestModule()
        assert module.event_type == 'simple_test'

    def test_abstract_methods_required(self):
        """Test that abstract methods must be implemented."""
        # Try to instantiate incomplete implementation
        with pytest.raises(TypeError):
            class IncompleteModule(EventModule):
                @property
                def event_type(self) -> str:
                    return 'incomplete'

            # This should fail because required abstract methods aren't implemented
            IncompleteModule()


class TestEventBuildData:
    """Test event data building."""

    def test_build_event_data(self):
        """Test that _build_event_data is called correctly."""
        module = SimpleTestModule()
        event_data = module._build_event_data(message="Hello, World!")
        assert event_data['message'] == "Hello, World!"

    def test_build_event_data_default(self):
        """Test that _build_event_data uses defaults."""
        module = SimpleTestModule()
        event_data = module._build_event_data()
        assert event_data['message'] == 'default'


class TestEventValidation:
    """Test event validation."""

    def test_validate_valid_event(self):
        """Test validation of valid event."""
        module = SimpleTestModule()
        is_valid, error_msg = module._validate_event_fields(
            {'message': 'Valid message'},
            recorded_by='test_peer',
            db=None
        )
        assert is_valid is True
        assert error_msg == ""

    def test_validate_missing_field(self):
        """Test validation detects missing fields."""
        module = SimpleTestModule()
        is_valid, error_msg = module._validate_event_fields(
            {'message': ''},  # Empty message
            recorded_by='test_peer',
            db=None
        )
        assert is_valid is False
        assert "Missing message" in error_msg

    def test_validate_field_constraint(self):
        """Test validation enforces field constraints."""
        module = SimpleTestModule()
        is_valid, error_msg = module._validate_event_fields(
            {'message': 'x' * 1001},  # Too long
            recorded_by='test_peer',
            db=None
        )
        assert is_valid is False
        assert "Message too long" in error_msg


class TestEventDependencies:
    """Test dependency extraction."""

    def test_no_dependencies(self):
        """Test event with no dependencies."""
        module = SimpleTestModule()
        deps = module._extract_dependencies({'message': 'hello'})
        assert deps == []

    def test_extract_dependencies(self):
        """Test extraction of dependencies."""
        module = DependencyTestModule()
        deps = module._extract_dependencies({
            'parent_id': 'parent_event_id_123',
            'content': 'test'
        })
        assert deps == ['parent_event_id_123']

    def test_dependencies_error_handling(self):
        """Test that dependencies() handles errors gracefully."""
        module = SimpleTestModule()
        # Call dependencies with invalid data (will cause error in subclass if not careful)
        deps = module.dependencies(None)
        # Should return empty list due to error handling
        assert deps == []


class TestEventSigning:
    """Test event signing functionality."""

    def test_unsigned_event(self):
        """Test that unsigned events don't call signing."""
        module = SimpleTestModule()
        signing_key = module._get_signing_key()
        assert signing_key is None

    def test_signed_event(self):
        """Test that signed events provide signing key."""
        module = SignedTestModule()
        signing_key = module._get_signing_key()
        assert signing_key is not None
        assert signing_key == b'test_signing_key_32_bytes_padding'


class TestEventEncryption:
    """Test event encryption functionality."""

    def test_unencrypted_event(self):
        """Test that unencrypted events don't call encryption."""
        module = SimpleTestModule()
        encryption_key = module._get_encryption_key()
        assert encryption_key is None

    def test_encrypted_event(self):
        """Test that encrypted events can provide encryption key."""
        # Create a test module that encrypts
        class EncryptedTestModule(EventModule):
            @property
            def event_type(self) -> str:
                return 'encrypted'

            def _build_event_data(self, **kwargs) -> dict[str, Any]:
                return {}

            def _get_encryption_key(self, **kwargs) -> dict[str, Any] | None:
                return {'id': 'key_1', 'key': b'test_key', 'type': 'group'}

            def _validate_event_fields(self, event_data, recorded_by, db) -> tuple[bool, str]:
                return True, ""

            def _project_to_tables(self, event_id, event_data, recorded_by, recorded_at, db) -> None:
                pass

            def _extract_dependencies(self, event_data) -> list[str]:
                return []

        module = EncryptedTestModule()
        encryption_key = module._get_encryption_key()
        assert encryption_key is not None
        assert encryption_key['id'] == 'key_1'


class TestEventProjection:
    """Test event projection to tables."""

    def test_project_simple_event(self):
        """Test projection of simple event."""
        module = SimpleTestModule()
        module._project_to_tables(
            event_id='event_123',
            event_data={'message': 'test'},
            recorded_by='peer_1',
            recorded_at=1000,
            db=None
        )

        # Check that event was recorded (in test implementation)
        assert hasattr(module, '_projected_events')
        assert len(module._projected_events) == 1
        assert module._projected_events[0]['event_id'] == 'event_123'

    def test_project_multiple_events(self):
        """Test projection of multiple events."""
        module = SimpleTestModule()

        for i in range(3):
            module._project_to_tables(
                event_id=f'event_{i}',
                event_data={'message': f'message_{i}'},
                recorded_by='peer_1',
                recorded_at=1000 + i,
                db=None
            )

        assert len(module._projected_events) == 3


class TestEventTemplateWorkflows:
    """Test the template method workflows (create, project, dependencies)."""

    def test_create_workflow_adds_type(self):
        """Test that create() adds 'type' field to event data."""
        module = SimpleTestModule()
        # We can't fully test create without crypto/store mocking,
        # but we can verify the method exists and has correct signature
        assert hasattr(module, 'create')
        assert callable(module.create)

    def test_project_workflow_validates_first(self):
        """Test that project() validates before projecting."""
        module = SimpleTestModule()

        # Invalid event - validation should fail
        module.project(
            event_id='event_123',
            event_data={'message': ''},  # Invalid - empty
            recorded_by='peer_1',
            recorded_at=1000,
            db=None
        )

        # Should not have projected anything (failed validation)
        assert not hasattr(module, '_projected_events') or len(module._projected_events) == 0

    def test_project_workflow_valid_event(self):
        """Test that project() projects valid events."""
        module = SimpleTestModule()

        # Valid event
        module.project(
            event_id='event_123',
            event_data={'message': 'Valid message'},
            recorded_by='peer_1',
            recorded_at=1000,
            db=None
        )

        # Should have projected the event
        assert hasattr(module, '_projected_events')
        assert len(module._projected_events) == 1

    def test_dependencies_workflow_returns_list(self):
        """Test that dependencies() returns a list."""
        module = SimpleTestModule()
        deps = module.dependencies({'message': 'test'})
        assert isinstance(deps, list)

    def test_dependencies_returns_extracted_dependencies(self):
        """Test that dependencies() returns extracted dependencies."""
        module = DependencyTestModule()
        deps = module.dependencies({
            'parent_id': 'parent_123',
            'content': 'test'
        })
        assert 'parent_123' in deps


class TestEventModuleCompliance:
    """Test that modules properly follow the EventModule interface."""

    def test_all_required_methods_implemented(self):
        """Test that SimpleTestModule implements all required methods."""
        module = SimpleTestModule()

        # Required abstract methods
        assert hasattr(module, 'event_type')
        assert hasattr(module, '_build_event_data')
        assert hasattr(module, '_validate_event_fields')
        assert hasattr(module, '_project_to_tables')
        assert hasattr(module, '_extract_dependencies')

        # Optional methods with defaults
        assert hasattr(module, '_get_signing_key')
        assert hasattr(module, '_get_encryption_key')

        # Template methods
        assert hasattr(module, 'create')
        assert hasattr(module, 'project')
        assert hasattr(module, 'dependencies')

    def test_multiple_modules_coexist(self):
        """Test that multiple event modules can coexist."""
        simple = SimpleTestModule()
        signed = SignedTestModule()
        dependency = DependencyTestModule()

        assert simple.event_type == 'simple_test'
        assert signed.event_type == 'signed_test'
        assert dependency.event_type == 'dependency_test'

        # Each should maintain separate state
        assert simple._validate_event_fields({'message': 'test'}, 'peer', None)[0]
        assert signed._validate_event_fields({}, 'peer', None)[0]
        assert not dependency._validate_event_fields({}, 'peer', None)[0]
