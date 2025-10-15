#!/usr/bin/env python3
"""Find all database writes outside of project() functions."""

import os
import re
from pathlib import Path

def find_execute_calls(root_dir='events'):
    """Find all .execute( calls and check if they're inside project() functions."""

    results = []

    for path in Path(root_dir).rglob('*.py'):
        with open(path, 'r') as f:
            lines = f.readlines()

        in_project_function = False
        current_function = None
        function_indent = 0

        for i, line in enumerate(lines, 1):
            # Check if we're entering a function definition
            func_match = re.match(r'^(\s*)def\s+(\w+)\s*\(', line)
            if func_match:
                function_indent = len(func_match.group(1))
                current_function = func_match.group(2)
                in_project_function = (current_function == 'project')
                continue

            # Check if we've dedented past the function (back to same or less indentation)
            if current_function:
                stripped = line.lstrip()
                if stripped and not stripped.startswith('#'):
                    leading_spaces = len(line) - len(stripped)
                    # If we're at or before the function indent level and it's not just whitespace
                    if leading_spaces <= function_indent and stripped:
                        current_function = None
                        in_project_function = False

            # Check for .execute( calls
            if '.execute(' in line:
                results.append({
                    'file': str(path),
                    'line': i,
                    'in_project': in_project_function,
                    'function': current_function,
                    'content': line.strip()
                })

    return results

if __name__ == '__main__':
    print("Searching for .execute() calls outside of project() functions...\n")

    results = find_execute_calls()

    # Separate into project and non-project
    in_project = [r for r in results if r['in_project']]
    outside_project = [r for r in results if not r['in_project']]

    print(f"Found {len(results)} total .execute() calls")
    print(f"  - {len(in_project)} inside project() functions")
    print(f"  - {len(outside_project)} OUTSIDE project() functions")
    print()

    if outside_project:
        print("=" * 80)
        print("WRITES OUTSIDE PROJECT() FUNCTIONS (may include legitimate create() writes):")
        print("=" * 80)

        # Group by function name
        by_function = {}
        for r in outside_project:
            func = r['function'] or '(module level)'
            if func not in by_function:
                by_function[func] = []
            by_function[func].append(r)

        for func_name in sorted(by_function.keys()):
            items = by_function[func_name]
            print(f"\n{func_name}: {len(items)} calls")
            for r in items[:3]:  # Show first 3 examples
                print(f"  {r['file']}:{r['line']}")
            if len(items) > 3:
                print(f"  ... and {len(items) - 3} more")
