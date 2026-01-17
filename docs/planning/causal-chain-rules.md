# Causal Chain Rules for Projector Testing

## Goal
Describe causal chains with data and conditions in a way that is easy to reason
about (human + LLM) and maps directly to our projector pipeline. The key idea is
to treat projectors as forward-chaining rules over immutable facts, then test
those rules and their chains with deterministic, explainable traces.

## Core Idea: Forward-Chaining Rules Over Facts
Model projection as a rule system:
- Facts: immutable events plus projected rows (materialized state).
- Rules: "requires" and "guards" over facts, then "emits" and "writes".
- Semantics: monotone, repeat until no new facts (fixpoint).

Blocked events are just rules whose required facts are missing. When a required
fact appears, the rule unblocks and can fire. This mirrors how projection_v2
blocks on missing deps and unblocks when those deps project.

## Academic Foundation (Short List)
This framing sits on well-established, deterministic foundations:
- Logic programming and Datalog: rule-based derivation with least fixpoint
  semantics for monotone rules.
- Production systems (OPS5, CLIPS) and Rete: forward-chaining execution of rules
  when facts appear, matching our "unblock then apply" behavior.
- Event Calculus and Situation Calculus: formal models of how events change
  state (fluents) over time.
- Dataflow and incremental view maintenance: materialized views updated by new
  facts, analogous to projection tables.
- Provenance / lineage: "why" and "how" a derived fact exists, used for
  explanations and verification of causal chains.

This is causal in the deterministic sense of "which facts cause which derived
facts," not probabilistic causal inference.

## Mapping to POC-6 Projectors
Direct correspondences in the current v2 pipeline:
- Facts: recorded events plus projection tables.
- Requires: EVENT_SPEC.requires in each event module.
- Guards: projector validation logic inside project_pure.
- Emits / Writes: ProjectorResult writes and derived rows.
- Blocked: resolver returns "block" when required deps are missing.
- Unblocked: same event projects later when deps appear in valid_events.

This makes projectors a concrete instantiation of forward-chaining rules.

## Rule Spec Shape (LLM-Friendly)
Keep a small, consistent "RuleSpec" alongside each projector. This does not
replace code, it documents intent and is easy to test.

Example (YAML-style, for humans and LLMs):
```
rule: route_ready
requires:
  - transit_prekey(conn_id)
  - station_active(station_id)
  - path_exists(conn_id, station_id)
guards:
  - same_network(conn_id, station_id)
  - signature_valid(signer, payload)
emits:
  - route_ready(route_id)
writes:
  - routes
```

Minimal mapping for POC-6:
- requires comes from EVENT_SPEC.requires
- guards are validations in project_pure
- writes are ProjectorResult writes
- emits are any follow-on events or durable rows

## Testing Patterns
Use rule tests and chain tests. Keep them deterministic and small.

1) Single rule tests
- Given required facts and inputs, expect specific writes.
- Assert guards reject invalid combinations.

2) Blocking tests
- Omit one required fact, assert "block" and no writes.
- Add the missing fact, re-run, assert success.

3) Chain tests (multi-step)
- Create a sequence where event B only becomes valid after event A projects.
- Assert that the chain produces the derived event only after the enabling fact
  is present.

4) Negative chain tests
- Provide alternate facts that almost satisfy guards; confirm no emission.

## Verification and Invariants
These properties make rule reasoning and LLM explanations reliable:
- Determinism: same inputs produce same outputs.
- Idempotence: re-projecting does not change results.
- Monotonicity: adding facts cannot invalidate prior derived facts.
- No hidden reads: projector logic depends only on ctx, not ad hoc DB queries.
- Traceability: each derived row can explain its upstream facts.

These map cleanly to unit and property tests. Provenance traces can be stored as
lightweight "because" lists in tests, even if not persisted in production.

## Example Chain Test Sketch
```
given:
  - invite_accepted(invite_id)
  - transit_prekey(conn_id, invite_id)
expect_blocked:
  - peer_shared(peer_id)  # missing invite material
then_add:
  - invite(invite_id, invite_pubkey)
expect:
  - peer_shared(peer_id)
because:
  - invite_accepted -> invite -> peer_shared
```

## Practical Adoption Steps
1) Add a RuleSpec block to new or complex projectors.
2) Use RuleSpec as the source of test cases for blocking and chain tests.
3) For chain tests, include a short "because" trace to keep explanations
   explicit for human and LLM review.

This keeps projector behavior principled, testable, and easy to reason about
without introducing new runtime machinery.
