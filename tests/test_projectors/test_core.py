"""Tests for core pure create functions (compute_event_id, etc)."""
import crypto
from projectors import compute_event_id, CreateResult, BlobSpec


class TestComputeEventId:
    """Test that event IDs are content-addressed."""

    def test_deterministic(self):
        """Same blob produces same ID."""
        blob = b'{"type": "test", "data": "hello"}'
        id1 = compute_event_id(blob)
        id2 = compute_event_id(blob)
        assert id1 == id2

    def test_different_blobs(self):
        """Different blobs produce different IDs."""
        blob1 = b'{"type": "test", "data": "hello"}'
        blob2 = b'{"type": "test", "data": "world"}'
        assert compute_event_id(blob1) != compute_event_id(blob2)

    def test_matches_crypto_hash(self):
        """Event ID is base64 of BLAKE2b hash."""
        blob = b'test blob content'
        expected = crypto.b64encode(crypto.hash(blob))
        assert compute_event_id(blob) == expected


class TestCreateResult:
    """Test CreateResult dataclass."""

    def test_empty_result(self):
        """Empty CreateResult has sensible defaults."""
        result = CreateResult()
        assert result.blobs == []
        assert result.primary_id == ""

    def test_with_blobs(self):
        """CreateResult with blobs."""
        blob = BlobSpec(blob=b'test', event_id='id_123', event_type='test')
        result = CreateResult(blobs=[blob], primary_id='id_123')
        assert len(result.blobs) == 1
        assert result.primary_id == 'id_123'


class TestBlobSpec:
    """Test BlobSpec dataclass."""

    def test_defaults(self):
        """BlobSpec has sensible defaults."""
        spec = BlobSpec(blob=b'test', event_id='id_123', event_type='test')
        assert spec.project is True

    def test_project_false(self):
        """BlobSpec can disable projection."""
        spec = BlobSpec(blob=b'test', event_id='id_123', event_type='test', project=False)
        assert spec.project is False
