"""Example parity test: legacy project() vs v2 project_pure().

This module demonstrates the pattern for projector-level TDD:
1. Build minimal DB state
2. Create an event
3. Project via legacy path and snapshot rows
4. (When v2 is ready) Project via v2 path and compare rows

For now, this shows the harness pattern. Full parity tests belong
in pilot conversion worktrees (proj-v2-admin, proj-v2-pilots-codex).
"""
import pytest
from core import crypto
from core.db import create_safe_db
from events.identity import network as network_module

from tests.projection_v2.helpers import (
    get_table_rows,
    compare_table_state,
    check_valid_event,
)


class TestNetworkProjectionParity:
    """Example: network event projection parity.

    Network events are simple (self-signed, no external deps) and make
    a good first example for the parity pattern.
    """

    def test_legacy_network_projection(self, v2_network_state):
        """Verify legacy network projection works as expected."""
        state = v2_network_state
        db = state['db']
        peer_id = state['peer_id']
        network_id = state['network_id']

        # Verify event is valid
        assert check_valid_event(network_id, peer_id, db)

        # Check networks table has the expected row
        rows = get_table_rows('networks', peer_id, db)
        assert len(rows) == 1

        network_row = rows[0]
        assert network_row['network_id'] == network_id
        assert network_row['recorded_by'] == peer_id
        assert network_row['network_pubkey'] == crypto.b64encode(state['network_pubkey'])

    def test_network_projection_expected_state(self, v2_network_state):
        """Document expected state after network projection."""
        state = v2_network_state
        db = state['db']
        peer_id = state['peer_id']
        network_id = state['network_id']

        expected_rows = [{
            'network_id': network_id,
            'recorded_by': peer_id,
            'network_pubkey': crypto.b64encode(state['network_pubkey']),
            'signed_by': network_id,  # Self-signed
        }]

        matches, msg = compare_table_state(
            'networks',
            peer_id,
            db,
            expected_rows,
            key_columns=['network_id', 'recorded_by', 'network_pubkey', 'signed_by'],
        )

        assert matches, f"Network table state mismatch: {msg}"


class TestParityPatternTemplate:
    """Template showing the full parity test pattern.

    Copy and adapt this for actual projector conversions.
    """

    def test_parity_pattern_placeholder(self, v2_db):
        """Placeholder showing parity test structure.

        Real parity tests follow this pattern:

        1. Setup:
           - Create minimal deps (network, user, peer_shared, etc.)
           - Ensure deps are projected and valid

        2. Create test event:
           - Build event with signer_type field
           - Store and record

        3. Legacy projection:
           - Call legacy project() or let recorded.project() dispatch
           - Snapshot relevant table rows

        4. Reset state (or use separate DB):
           - Clear projection tables
           - Keep event in store

        5. V2 projection:
           - Call resolve_event() to get context
           - Call project_pure() to get WriteOps
           - Apply writes manually or via apply_writes()

        6. Compare:
           - Compare table snapshots
           - Assert rows match
        """
        # This test just documents the pattern
        # Real parity tests in pilot conversion worktrees
        assert True


class TestProjectorLevelTDD:
    """TDD pattern for new projector conversions.

    Write tests BEFORE implementing project_pure().
    Tests initially fail, then pass as implementation progresses.
    """

    def test_tdd_example_structure(self, v2_network_state):
        """Example TDD test structure for projector conversion.

        Step 1: Write this test describing expected behavior
        Step 2: Implement project_pure() to make it pass
        Step 3: Run parity test to verify legacy and v2 match
        """
        state = v2_network_state
        db = state['db']
        peer_id = state['peer_id']
        network_id = state['network_id']

        # For a real projector TDD test:
        # 1. Build event_data as project_pure would receive it
        from core.projection_v2.types import ProjectionContext, WriteOp

        ctx = ProjectionContext(
            event_id=network_id,
            event_type='network',
            event_data={
                'type': 'network',
                'network_pubkey': crypto.b64encode(state['network_pubkey']),
                'created_at': 1000,
            },
            recorded_by=peer_id,
            recorded_at=1000,
            deps={},
            signer=None,
        )

        # 2. Define expected WriteOps (what project_pure should return)
        expected_writes = [
            WriteOp(
                op='insert',
                table='networks',
                values={
                    'network_id': network_id,
                    'creator_user_id': '',
                    'network_pubkey': crypto.b64encode(state['network_pubkey']),
                    'signed_by': network_id,
                    'created_at': 1000,
                    'recorded_by': peer_id,
                    'recorded_at': 1000,
                },
            )
        ]

        # 3. When project_pure is implemented, call it and compare:
        # result = network_module.project_pure(ctx)
        # assert result.writes == tuple(expected_writes)

        # For now, verify the context structure is valid
        assert ctx.event_type == 'network'
        assert ctx.event_id == network_id
