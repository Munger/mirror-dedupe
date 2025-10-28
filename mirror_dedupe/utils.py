#!/usr/bin/env python3
"""
utils.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import os
import sys
import shutil
import threading
from typing import Tuple

# PID file location (will be set based on mode)
PID_FILE = None

# Global counters for download tracking
active_downloads = 0
download_lock = threading.Lock()


def acquire_lock(lock_name='main'):
    """Acquire PID file lock to prevent multiple instances"""
    global PID_FILE
    PID_FILE = f'/var/run/mirror_dedupe.{lock_name}.pid'
    
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            # Check if process is still running
            try:
                os.kill(old_pid, 0)
                print(f"ERROR: Another instance is already running for '{lock_name}' (PID {old_pid})")
                print(f"If this is incorrect, remove {PID_FILE} and try again.")
                return False
            except OSError:
                # Process not running, remove stale PID file
                print(f"Removing stale PID file for '{lock_name}' (PID {old_pid} not running)")
                os.remove(PID_FILE)
        except (ValueError, IOError):
            print(f"Warning: Invalid PID file for '{lock_name}', removing")
            os.remove(PID_FILE)
    
    # Write our PID
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    print(f"Acquired lock for '{lock_name}' (PID {os.getpid()})")
    return True


def release_lock():
    """Release PID file lock"""
    global PID_FILE
    try:
        if PID_FILE and os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            print(f"Released lock")
    except:
        pass


def signal_handler(signum, frame):
    """Handle termination signals"""
    print(f"\nReceived signal {signum}, cleaning up...")
    release_lock()
    sys.exit(1)


def get_disk_usage(path: str) -> Tuple[int, int, int]:
    """Get disk usage for a path"""
    try:
        stat = shutil.disk_usage(path)
        return (stat.total, stat.used, stat.free)
    except Exception as e:
        print(f"  Warning: Could not get disk usage for {path}: {e}")
        return (0, 0, 0)


def format_bytes(bytes_val: int) -> str:
    """Format bytes as human-readable string"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"


def calculate_total_hardlink_savings(mirrors: list) -> Tuple[int, int]:
    """Calculate total space saved by existing hardlinks across all mirrors"""
    total_files = 0
    total_bytes = 0
    
    # Build a map of inodes to (size, paths)
    inode_map = {}
    
    for mirror in mirrors:
        dest = mirror['dest']
        pool_dir = os.path.join(dest, 'pool')
        
        if not os.path.exists(pool_dir):
            continue
        
        # Walk pool directory
        for root, dirs, files in os.walk(pool_dir):
            for filename in files:
                filepath = os.path.join(root, filename)
                try:
                    stat = os.stat(filepath)
                    inode = stat.st_ino
                    size = stat.st_size
                    
                    if inode not in inode_map:
                        inode_map[inode] = {'size': size, 'paths': []}
                    inode_map[inode]['paths'].append(filepath)
                except:
                    pass
    
    # Calculate savings from hardlinks
    for inode, info in inode_map.items():
        num_links = len(info['paths'])
        if num_links > 1:
            # Space saved = (n-1) * file_size
            total_files += (num_links - 1)
            total_bytes += (num_links - 1) * info['size']
    
    return (total_files, total_bytes)
