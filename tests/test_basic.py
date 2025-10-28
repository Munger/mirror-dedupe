#!/usr/bin/env python3
"""
test_basic.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_imports():
    """Test that all modules can be imported"""
    print("Testing imports...")
    
    try:
        from mirror_dedupe import cli
        print("  ✓ CLI module")
    except Exception as e:
        print(f"  ✗ CLI module: {e}")
        return False
    
    try:
        from mirror_dedupe import config
        print("  ✓ Config module")
    except Exception as e:
        print(f"  ✗ Config module: {e}")
        return False
    
    try:
        from mirror_dedupe import orchestrate
        print("  ✓ Orchestrate module")
    except Exception as e:
        print(f"  ✗ Orchestrate module: {e}")
        return False
    
    try:
        from mirror_dedupe import utils
        print("  ✓ Utils module")
    except Exception as e:
        print(f"  ✗ Utils module: {e}")
        return False
    
    try:
        from mirror_dedupe import indices
        print("  ✓ Indices module")
    except Exception as e:
        print(f"  ✗ Indices module: {e}")
        return False
    
    try:
        from mirror_dedupe import download
        print("  ✓ Download module")
    except Exception as e:
        print(f"  ✗ Download module: {e}")
        return False
    
    try:
        from mirror_dedupe import dedupe
        print("  ✓ Dedupe module")
    except Exception as e:
        print(f"  ✗ Dedupe module: {e}")
        return False
    
    try:
        from mirror_dedupe import sync
        print("  ✓ Sync module")
    except Exception as e:
        print(f"  ✗ Sync module: {e}")
        return False
    
    return True


def test_config_loading():
    """Test configuration loading"""
    print("\nTesting config loading...")
    
    try:
        from mirror_dedupe.config import load_config
        
        config_dir = os.path.join(os.path.dirname(__file__), 'test-config')
        if not os.path.exists(config_dir):
            print(f"  ⚠ Config directory not found: {config_dir}")
            return True  # Not a failure, just not present
        
        config = load_config(config_dir)
        mirrors = config.get('mirrors', [])
        
        print(f"  ✓ Loaded {len(mirrors)} mirror(s)")
        
        # Check config structure
        if 'repo_root' in config:
            print(f"  ✓ repo_root: {config['repo_root']}")
        
        for mirror in mirrors:
            name = mirror.get('name', 'unknown')
            print(f"    - {name}")
        
        return True
    except Exception as e:
        print(f"  ✗ Config loading failed: {e}")
        return False


def test_utility_functions():
    """Test utility functions"""
    print("\nTesting utility functions...")
    
    try:
        from mirror_dedupe.utils import format_bytes
        
        result = format_bytes(1234567890)
        print(f"  ✓ format_bytes(1234567890) = {result}")
        
        return True
    except Exception as e:
        print(f"  ✗ Utility functions failed: {e}")
        return False


def test_dedupe_functions():
    """Test dedupe functions"""
    print("\nTesting dedupe functions...")
    
    try:
        from mirror_dedupe.dedupe import expand_distributions
        
        result = expand_distributions(['noble'])
        expected = ['noble', 'noble-updates', 'noble-security', 'noble-backports', 'noble-proposed']
        
        if result == expected:
            print(f"  ✓ expand_distributions(['noble']) = {result}")
        else:
            print(f"  ✗ expand_distributions(['noble']) returned unexpected: {result}")
            return False
        
        return True
    except Exception as e:
        print(f"  ✗ Dedupe functions failed: {e}")
        return False


def test_syntax():
    """Test Python syntax of all modules"""
    print("\nTesting Python syntax...")
    
    import py_compile
    
    base_dir = os.path.dirname(os.path.dirname(__file__))
    module_dir = os.path.join(base_dir, 'mirror_dedupe')
    
    if not os.path.exists(module_dir):
        print(f"  ✗ Module directory not found: {module_dir}")
        return False
    
    all_ok = True
    for filename in os.listdir(module_dir):
        if filename.endswith('.py'):
            filepath = os.path.join(module_dir, filename)
            try:
                py_compile.compile(filepath, doraise=True)
                print(f"  ✓ {filename}")
            except py_compile.PyCompileError as e:
                print(f"  ✗ {filename}: {e}")
                all_ok = False
    
    return all_ok


def main():
    """Run all tests"""
    print("=" * 60)
    print("Mirror-Dedupe Basic Tests")
    print("=" * 60)
    
    tests = [
        ("Imports", test_imports),
        ("Config Loading", test_config_loading),
        ("Utility Functions", test_utility_functions),
        ("Dedupe Functions", test_dedupe_functions),
        ("Python Syntax", test_syntax),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n✗ {name} crashed: {e}")
            results.append((name, False))
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n✓ All tests passed!")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
