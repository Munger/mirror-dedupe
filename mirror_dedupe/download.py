#!/usr/bin/env python3
"""
download.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import os
import subprocess
import hashlib
from .utils import download_lock, active_downloads

# Check if sha256sum is available at startup
USE_SHA256SUM = False
try:
    result = subprocess.run(['sha256sum', '--version'], capture_output=True)
    USE_SHA256SUM = result.returncode == 0
except:
    pass


def download_with_curl(url: str, dest_path: str, timeout: int = 300, progress_info: str = "") -> bool:
    """Download file with curl, supports resuming partial downloads"""
    from . import utils
    
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)
    
    # Show what we're downloading
    filename = os.path.basename(dest_path)
    with utils.download_lock:
        utils.active_downloads += 1
        current_active = utils.active_downloads
    print(f"  -> Downloading: {filename} ({current_active} active){progress_info}", flush=True)
    
    try:
        # -C - enables automatic resume of partial downloads
        cmd = ['curl', '-f', '-L', '-C', '-', '--max-time', str(timeout), '-o', dest_path, url]
        result = subprocess.run(cmd, capture_output=True)
        
        with utils.download_lock:
            utils.active_downloads -= 1
            remaining = utils.active_downloads
        
        if result.returncode == 0:
            print(f"  [OK] Completed: {filename} ({remaining} remaining)", flush=True)
        else:
            print(f"  [FAIL] Failed: {filename} ({remaining} remaining)", flush=True)
        
        return result.returncode == 0
    except Exception as e:
        with utils.download_lock:
            utils.active_downloads -= 1
            remaining = utils.active_downloads
        print(f"  [ERROR] Error: {filename} ({remaining} remaining) - {e}", flush=True)
        return False


def verify_sha256(file_path: str, expected_hash: str, buffer_size: int = 1048576) -> bool:
    """Verify file SHA256 hash using sha256sum or Python hashlib"""
    if USE_SHA256SUM:
        # Use fast sha256sum command
        try:
            result = subprocess.run(['sha256sum', file_path], 
                                  capture_output=True, text=True)
            if result.returncode == 0:
                actual_hash = result.stdout.split()[0]
                return actual_hash == expected_hash
            return False
        except:
            return False
    else:
        # Use Python hashlib
        try:
            sha256 = hashlib.sha256()
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(buffer_size), b''):
                    sha256.update(chunk)
            return sha256.hexdigest() == expected_hash
        except:
            return False
