#!/usr/bin/env python3
"""
Master test runner for all command tests.
Runs all command tests and reports results.
"""
import sys
import traceback
from typing import List, Tuple


def run_test_module(module_name: str, test_name: str) -> Tuple[bool, str]:
    """Run tests from a module and return success status."""
    try:
        # Import the module directly
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

        # Import the module from the correct path
        module_path = f'protocols.quiet.events.{module_name}.test_command'
        module = __import__(module_path, fromlist=['run_tests'])
        module.run_tests()
        return True, f"✓ {test_name} tests passed"
    except Exception as e:
        error_msg = f"✗ {test_name} tests failed: {str(e)}"
        traceback.print_exc()
        return False, error_msg


def main():
    """Run all command tests."""
    print("\n" + "=" * 70)
    print("                    RUNNING ALL COMMAND TESTS")
    print("=" * 70)
    
    # List of test modules to run
    test_modules = [
        ('identity', 'Identity'),
        ('peer', 'Peer'),
        ('user', 'User'),
        ('link_invite', 'Link Invite'),
        ('address', 'Address'),
        ('group', 'Group'),
        ('channel', 'Channel'),
        ('message', 'Message'),
    ]
    
    results: List[Tuple[bool, str]] = []
    
    for module_name, test_name in test_modules:
        print(f"\n{'=' * 70}")
        print(f"Running {test_name} Command Tests...")
        print('=' * 70)
        
        success, message = run_test_module(module_name, test_name)
        results.append((success, message))
        
        if not success:
            print(f"\n⚠️  {message}")
    
    # Print summary
    print("\n" + "=" * 70)
    print("                        TEST SUMMARY")
    print("=" * 70)
    
    passed = sum(1 for success, _ in results if success)
    failed = len(results) - passed
    
    for success, message in results:
        if success:
            print(f"  {message}")
        else:
            print(f"  {message}")
    
    print("\n" + "-" * 70)
    print(f"Total: {len(results)} test suites | Passed: {passed} | Failed: {failed}")
    print("-" * 70)
    
    if failed > 0:
        print("\n❌ Some tests failed. Please review the errors above.")
        sys.exit(1)
    else:
        print("\n✅ All command tests passed successfully!")
        sys.exit(0)


if __name__ == "__main__":
    main()
