# CC Test Review Notes

Notes on `tests/test_congestion_control.py`:

- Nondeterministic: simulator loss uses RNG and tests don't seed it, so assertions may be flaky.
- `TestRTTMeasurement` has no assertions and will always pass (just prints).
- Recovery test resets `sim.config` and `sim.packets_sent` but not `packets_dropped`/pending queue/burst state; prefer `sim.reset()` or a fresh simulator.
- CC state is module-level (`events/network/negentropy.py::_cc_state`) and isn't reset between tests; add a reset hook or fixture.
- Packet counts are transport-level totals and include non-negentropy traffic; could cause false positives/negatives.
- `run_sync_rounds` uses 1000ms ticks (receive runs at 100ms), so behavior differs from production timing.
- Some assertions are conditional (skip if counts are 0); failures could be silently ignored.
