"""Tests for group_key.create_pure() - deterministic blob."""
import crypto
from projectors import compute_event_id
from projectors.group_key import create_pure


class TestGroupKeyCreatePure:
    """Test group_key.create_pure() - deterministic blob."""

    def test_no_timestamp_in_blob(self):
        """Group key blob has no timestamp for determinism."""
        key_material = crypto.generate_secret()
        deps = {'key_material': key_material}

        result = create_pure(deps)

        blob_data = crypto.parse_json(result.blobs[0].blob)
        assert 'created_at' not in blob_data
        assert blob_data['type'] == 'group_key'

    def test_same_material_same_id(self):
        """Same key material produces same key_id (deterministic)."""
        key_material = crypto.generate_secret()

        result1 = create_pure({'key_material': key_material})
        result2 = create_pure({'key_material': key_material})

        assert result1.primary_id == result2.primary_id

    def test_different_material_different_id(self):
        """Different key material produces different key_id."""
        result1 = create_pure({'key_material': crypto.generate_secret()})
        result2 = create_pure({'key_material': crypto.generate_secret()})

        assert result1.primary_id != result2.primary_id

    def test_id_is_content_addressed(self):
        """Event ID matches hash of blob."""
        key_material = crypto.generate_secret()
        result = create_pure({'key_material': key_material})

        expected_id = compute_event_id(result.blobs[0].blob)
        assert result.primary_id == expected_id
