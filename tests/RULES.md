# Test Writing Rules

Permanent guidelines for writing tests in this codebase.

## DO

- **Use API-only queries in scenario tests** - Use query functions to verify behavior, never inspect database directly
- **Use deterministic time** - Pass explicit `t_ms` values, never call `time.time()` in tests
- **Use tick() for sync** - Call `tick()` to drive sync operations deterministically
- **Test convergence** - When testing multi-peer scenarios, assert convergence is reached
- **Use descriptive test names** - `test_<feature>_<scenario>.py` pattern clarifies intent
- **Use fixtures from conftest.py** - Don't recreate database/network setup code
- **Isolate test state** - Each test should be independent, no shared state between tests

## DON'T

- **Don't import internal implementation details** - Use public APIs and query functions
- **Don't create long-running manual setups** - Use fixtures or helpers instead
- **Don't assume test execution order** - Tests run in any order; setup/cleanup must be in each test
- **Don't call time.time()** - Always use `t_ms` parameter for deterministic testing
- **Don't inspect internals for assertions** - Use query functions to verify state
- **Don't commit temporary debugging code** - Remove or move to debug scripts first
- **Don't add sleep() or timeouts** - Deterministic time makes them unnecessary

## Key Principles

1. **Deterministic** - Tests must produce same results every run
2. **Fast** - Use tick() instead of real time or sleeps
3. **Isolated** - No test should depend on another test's state
4. **Clear** - Test names and structure should immediately show what's being tested
5. **Comprehensive** - Test both happy path and error conditions
