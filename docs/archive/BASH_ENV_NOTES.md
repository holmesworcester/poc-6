# Bash Environment Variable Issue

## Problem
When running commands like:
```bash
PYTHONPATH=. ./venv/bin/pytest tests/scenario_tests/test_three_player_messaging.py -xvs
```

You may get the error:
```
/bin/bash: line 1: PYTHONPATH=.: command not found
```

## Root Cause
In certain shell contexts (particularly when using pipes or command substitution), bash interprets `PYTHONPATH=.` as a command rather than an environment variable assignment prefix.

## Solutions

### 1. Use `export` (Two Commands)
```bash
export PYTHONPATH=.
./venv/bin/pytest tests/scenario_tests/test_three_player_messaging.py -xvs
```

### 2. Use `env` Command
```bash
env PYTHONPATH=. ./venv/bin/pytest tests/scenario_tests/test_three_player_messaging.py -xvs
```

### 3. Use Subshell
```bash
(PYTHONPATH=. ./venv/bin/pytest tests/scenario_tests/test_three_player_messaging.py -xvs)
```

### 4. Use `bash -c`
```bash
bash -c 'PYTHONPATH=. ./venv/bin/pytest tests/scenario_tests/test_three_player_messaging.py -xvs'
```

## Why This Happens
- Direct environment variable assignment syntax (`VAR=value command`) works in interactive shells
- But in non-interactive contexts or with certain shell options, it may be parsed differently
- The space after `PYTHONPATH=.` causes bash to treat it as a separate command
- This is especially common when the command is passed through multiple layers of shell interpretation

## Best Practice for Scripts
For scripts and automated contexts, prefer:
1. `env` command for portability
2. `export` for clarity
3. Explicit shell invocation with `bash -c` when needed