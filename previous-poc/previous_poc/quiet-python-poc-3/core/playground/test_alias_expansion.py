"""Simple synchronous tester for playground alias/default expansion.

This script is used to validate how the playground YAML's aliases and
default_command will be expanded. It does not invoke any API or async
code, so it can be used in restricted environments.
"""
import sys
import yaml

def load_config(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

def expand_command(state, command):
    # Variable substitution
    for var, value in state.get('variables', {}).items():
        command = command.replace(f"{{{var}}}", value)

    # If command is a slash command, strip leading /
    is_slash = command.startswith('/')
    if is_slash:
        cmd_body = command[1:]
    else:
        cmd_body = command

    # First check aliases
    parts = cmd_body.split(None, 1)
    if parts and parts[0] in state.get('aliases', {}):
        alias_cmd = state['aliases'][parts[0]]
        if len(parts) > 1:
            args = parts[1]
            # substitute positional args
            arg_list = args.split()
            for i, arg in enumerate(arg_list):
                alias_cmd = alias_cmd.replace(f"{{{i+1}}}", arg)
            alias_cmd = alias_cmd.replace('{*}', args)
        return alias_cmd

    # No alias matched; apply default_command if present and not a slash
    if not is_slash and state.get('default_command'):
        return state['default_command'].replace('{input}', command)

    # Otherwise return the command unchanged
    return command

def main():
    if len(sys.argv) < 3:
        print("Usage: test_alias_expansion.py <config.yaml> <command> [<command> ...]")
        sys.exit(1)

    cfg = load_config(sys.argv[1])
    # Use first window by default for testing
    windows = cfg.get('windows', [])
    if not windows:
        print("No windows defined in config")
        sys.exit(1)

    state = {
        'aliases': windows[0].get('aliases', {}),
        'variables': windows[0].get('variables', {}),
        'default_command': windows[0].get('default_command')
    }

    for cmd in sys.argv[2:]:
        expanded = expand_command(state, cmd)
        print(f"> {cmd}\n=> {expanded}\n")

if __name__ == '__main__':
    main()
