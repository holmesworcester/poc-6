"""Base class for event modules enforcing consistent create/project/dependencies pattern."""
from abc import ABC, abstractmethod
from typing import Any
import logging

log = logging.getLogger(__name__)


class EventModule(ABC):
    """Base class for event modules.

    Enforces consistent create/project/dependencies pattern.
    Subclasses implement domain-specific logic for:
    - Building event data
    - Signing/encrypting events (if needed)
    - Validating event fields
    - Projecting events to tables
    - Extracting dependencies

    This class handles the boilerplate:
    - Standard create workflow (sign → encrypt → store)
    - Standard project workflow (validate → insert → record dependencies)
    - Standard dependencies workflow (error handling)
    """

    @property
    @abstractmethod
    def event_type(self) -> str:
        """Event type name (e.g., 'message', 'group_key').

        Returns:
            String identifier for this event type
        """
        pass

    @abstractmethod
    def _build_event_data(self, **kwargs) -> dict[str, Any]:
        """Build event data dict from parameters.

        Must set 'type' field to self.event_type.

        Args:
            **kwargs: Event-specific parameters

        Returns:
            Event data dict ready for signing/encryption
        """
        pass

    def _get_signing_key(self, **kwargs) -> bytes | None:
        """Get the key to sign this event with.

        Override in subclass if event is signed.
        Return None if event is not signed.

        Default: Not signed.

        Args:
            **kwargs: Event-specific parameters

        Returns:
            Signing key bytes, or None if not signed
        """
        return None

    def _get_encryption_key(self, **kwargs) -> dict[str, Any] | None:
        """Get the key to encrypt this event with.

        Override in subclass if event is encrypted.
        Return transit key dict or None if not encrypted.

        Default: Not encrypted.

        Args:
            **kwargs: Event-specific parameters

        Returns:
            Key dict {id, key, type} or None if not encrypted
        """
        return None

    @abstractmethod
    def _validate_event_fields(self, event_data: dict[str, Any], recorded_by: str, db: Any) -> tuple[bool, str]:
        """Validate event fields and business logic.

        Called after event is decrypted/unwrapped.
        Return (is_valid, error_message).

        Args:
            event_data: Decrypted event data
            recorded_by: Peer who recorded this
            db: Database connection

        Returns:
            (is_valid: bool, error_msg: str)
        """
        pass

    @abstractmethod
    def _project_to_tables(self, event_id: str, event_data: dict[str, Any], recorded_by: str, recorded_at: int, db: Any) -> None:
        """Insert event into appropriate database tables.

        Called after validation passes.

        Args:
            event_id: Event ID
            event_data: Validated event data
            recorded_by: Peer who recorded this
            recorded_at: Timestamp
            db: Database connection
        """
        pass

    @abstractmethod
    def _extract_dependencies(self, event_data: dict[str, Any]) -> list[str]:
        """Extract dependency event IDs from event data.

        Args:
            event_data: Event data dict

        Returns:
            List of event IDs this depends on
        """
        pass

    # ========================================================================
    # Template methods (non-overridable, implement the standard workflows)
    # ========================================================================

    def create(self, peer_id: str, t_ms: int, db: Any, **kwargs) -> dict[str, Any]:
        """Create event with standard workflow.

        Workflow:
        1. Build event data
        2. Sign if needed
        3. Encrypt if needed
        4. Store in event log

        Args:
            peer_id: Creator's peer ID
            t_ms: Timestamp in milliseconds
            db: Database connection
            **kwargs: Event-specific parameters passed to _build_event_data and _get_*_key methods

        Returns:
            Dict with event ID: {'id': event_id, ...}
        """
        # Step 1: Build event data
        event_data = self._build_event_data(**kwargs)
        event_data['type'] = self.event_type

        # Step 2: Sign if needed
        signing_key = self._get_signing_key(**kwargs)
        if signing_key:
            import crypto
            event_data = crypto.sign_event(event_data, signing_key)
            log.debug(f"{self.event_type}.create() signed event with provided key")

        # Step 3: Prepare for storage (canonicalize)
        import crypto
        canonical = crypto.canonicalize_json(event_data)

        # Step 4: Encrypt if needed
        encryption_key = self._get_encryption_key(**kwargs)
        if encryption_key:
            blob = crypto.wrap(canonical, encryption_key, db)
            log.debug(f"{self.event_type}.create() encrypted event with provided key")
        else:
            blob = canonical

        # Step 5: Store in event log
        import store
        event_id = store.event(blob, peer_id, t_ms, db)

        log.debug(f"{self.event_type}.create() created {event_id[:20]}...")
        return {'id': event_id}

    def project(self, event_id: str, event_data: dict[str, Any], recorded_by: str, recorded_at: int, db: Any) -> None:
        """Project event with standard workflow.

        Workflow:
        1. Validate fields and business logic
        2. Project to appropriate tables
        3. Record dependencies

        Args:
            event_id: Event ID
            event_data: Decrypted event data
            recorded_by: Peer who recorded this
            recorded_at: Timestamp
            db: Database connection
        """
        # Step 1: Validate
        is_valid, error_msg = self._validate_event_fields(event_data, recorded_by, db)
        if not is_valid:
            log.warning(f"{self.event_type}.project() validation failed: {error_msg}")
            return

        # Step 2: Project to tables
        self._project_to_tables(event_id, event_data, recorded_by, recorded_at, db)
        log.debug(f"{self.event_type}.project() projected {event_id[:20]}...")

        # Step 3: Record dependencies
        from db import create_safe_db
        safedb = create_safe_db(db, recorded_by=recorded_by)
        for parent_event_id in self._extract_dependencies(event_data):
            safedb.execute(
                """INSERT OR IGNORE INTO event_dependencies
                   (child_event_id, parent_event_id, recorded_by, dependency_type)
                   VALUES (?, ?, ?, ?)""",
                (event_id, parent_event_id, recorded_by, self.event_type)
            )
        log.debug(f"{self.event_type}.project() recorded dependencies for {event_id[:20]}...")

    def dependencies(self, event_data: dict[str, Any]) -> list[str]:
        """Get dependencies with standard error handling.

        Args:
            event_data: Event data dict

        Returns:
            List of event IDs this depends on, or empty list on error
        """
        try:
            return self._extract_dependencies(event_data)
        except Exception as e:
            log.warning(f"{self.event_type}.dependencies() error: {e}")
            return []
