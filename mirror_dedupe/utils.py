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
import subprocess
import re
from urllib.parse import urlparse, urljoin
from typing import Tuple, Optional, Dict

# PID file location (will be set based on mode)
PID_FILE = None

# Global counters for download tracking
active_downloads = 0
download_lock = threading.Lock()


# Cached per-host IPv6 health checks so we only probe each host once per run.
_ipv6_host_cache = {}


def analyze_url_connectivity(url: str, timeout: int = 5) -> Dict[str, any]:
    """
    Analyze URL connectivity for both IPv4 and IPv6 with detailed error classification.
    
    Returns a dictionary with:
    - reachable: bool (can we connect at all?)
    - ipv6_reachable: bool (specifically IPv6)
    - ipv4_reachable: bool (specifically IPv4)
    - status_code: int (HTTP status if available)
    - final_url: str (after redirects)
    - error_type: str (classification of the issue)
    - recommended_action: str (what to do about it)
    """
    result = {
        'reachable': False,
        'ipv6_reachable': False,
        'ipv4_reachable': False,
        'status_code': None,
        'final_url': url,
        'error_type': None,
        'recommended_action': None
    }
    
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    
    if scheme not in ("http", "https"):
        result['reachable'] = True
        result['final_url'] = url
        result['recommended_action'] = 'Non-HTTP URL, use as-is'
        return result
    
    # Test IPv6 first
    ipv6_result = _test_http_connectivity(url, timeout, ipv6=True)
    if ipv6_result['reachable']:
        result.update(ipv6_result)
        result['ipv6_reachable'] = True
        return result
    
    # If IPv6 failed, test IPv4
    ipv4_result = _test_http_connectivity(url, timeout, ipv6=False)
    if ipv4_result['reachable']:
        result.update(ipv4_result)
        result['ipv4_reachable'] = True
        return result
    
    # Both failed
    result['error_type'] = 'connection_failed'
    result['recommended_action'] = 'URL unreachable, check host and network'
    return result


def _test_http_connectivity(url: str, timeout: int = 5, ipv6: bool = True) -> Dict[str, any]:
    """Test HTTP connectivity for either IPv6 or IPv4"""
    result = {
        'reachable': False,
        'status_code': None,
        'final_url': url,
        'error_type': None,
        'recommended_action': None
    }
    
    try:
        # Use curl with proper flags
        curl_flags = ['-6'] if ipv6 else ['-4']
        test_url = url if url.endswith('/') else url + '/'
        
        curl_cmd = [
            'curl',
            *curl_flags,
            '-s',
            '-I',
            '-L',  # Follow redirects
            '-w', '%{http_code}\n%{redirect_url}\n',
            '-o', '/dev/null',
            '--max-time', str(timeout),
            test_url
        ]
        
        process = subprocess.run(curl_cmd, capture_output=True, text=True)
        
        if process.returncode == 0:
            result['reachable'] = True
            output = process.stdout.strip().split('\n')
            
            if len(output) >= 1:
                result['status_code'] = int(output[0])
            if len(output) >= 2 and output[1]:
                result['final_url'] = urljoin(url, output[1])
            
            # Classify the status
            status = result['status_code']
            if status and 300 <= status < 400:
                result['error_type'] = 'redirect'
                result['recommended_action'] = f'Use redirected URL: {result["final_url"]}'
            elif status and 400 <= status < 500:
                if status == 401:
                    result['error_type'] = 'auth_required'
                    result['recommended_action'] = 'Authentication required, check credentials'
                elif status == 403:
                    result['error_type'] = 'forbidden'
                    result['recommended_action'] = 'Access forbidden, may need different path or auth'
                elif status == 404:
                    result['error_type'] = 'not_found'
                    result['recommended_action'] = 'Path not found, URL may need correction'
                else:
                    result['error_type'] = 'client_error'
                    result['recommended_action'] = f'Client error {status}, check request format'
            elif status and 500 <= status < 600:
                result['error_type'] = 'server_error'
                result['recommended_action'] = f'Server error {status}, retry later or contact admin'
            else:
                result['error_type'] = 'success'
                result['recommended_action'] = 'URL is accessible'
        else:
            result['error_type'] = 'connection_failed'
            result['recommended_action'] = f'Connection failed ({"IPv6" if ipv6 else "IPv4"})'
    
    except Exception as e:
        result['error_type'] = 'exception'
        result['recommended_action'] = f'Exception occurred: {str(e)}'
    
    return result


def get_optimal_url(url: str, timeout: int = 5) -> tuple:
    """
    Get the optimal URL and connectivity information.
    
    Returns: (optimal_url, connectivity_info)
    """
    info = analyze_url_connectivity(url, timeout)
    
    if not info['reachable']:
        return (None, info)
    
    # If we have a redirect, use the final URL
    if info['final_url'] != url:
        return (info['final_url'], info)
    
    return (url, info)


def classify_url_issue(url: str, timeout: int = 5) -> str:
    """
    Classify the type of issue with a URL.
    
    Returns one of:
    - 'working' - URL is accessible
    - 'redirect' - URL redirects to another location
    - 'path_issue' - IPv6 works but path is wrong (404/403)
    - 'ipv6_broken' - IPv6 fails but IPv4 works
    - 'completely_broken' - Neither IPv4 nor IPv6 works
    - 'server_issue' - Server is having problems (5xx)
    """
    info = analyze_url_connectivity(url, timeout)
    
    if not info['reachable']:
        return 'completely_broken'
    
    if info['error_type'] == 'redirect':
        return 'redirect'
    
    if info['error_type'] in ['not_found', 'forbidden', 'auth_required']:
        return 'path_issue'
    
    if info['error_type'] == 'server_error':
        return 'server_issue'
    
    if info['ipv4_reachable'] and not info['ipv6_reachable']:
        return 'ipv6_broken'
    
    return 'working'


def get_corrected_url(url: str, timeout: int = 5) -> Optional[str]:
    """Extract the correct URL from redirects and status codes.
    
    Returns the final URL after following redirects, or None if IPv6 fails.
    For 404/403 responses, returns the original URL (IPv6 works, path issue).
    """
    if not url:
        return None
        
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    
    if scheme not in ("http", "https"):
        return url  # Non-HTTP URLs, return as-is
        
    try:
        # Test with IPv6, using curl's output formatting to get clean data
        test_url = url if url.endswith('/') else url + '/'
        result = subprocess.run(
            [
                "curl",
                "-6",
                "-s",
                "-I",
                "-w", "%{http_code}\n%{redirect_url}\n",
                "-o", "/dev/null",  # Discard headers
                "--max-time",
                str(timeout),
                test_url,
            ],
            capture_output=True,
            text=True,
        )
        
        if result.returncode != 0:
            # IPv6 connection failed
            return None
            
        # Parse curl's formatted output
        output = result.stdout.strip().split('\n')
        if len(output) >= 2:
            try:
                status_code = int(output[0])
                redirect_url = output[1].strip()
                
                # If we got a redirect (3xx) and have a redirect URL, use it
                if status_code and 300 <= status_code < 400 and redirect_url:
                    return urljoin(url, redirect_url)
                else:
                    return url
            except (ValueError, IndexError):
                return url
        else:
            return url
        
    except Exception:
        return None


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


def ipv6_available(url: str, timeout: int = 5) -> bool:
    """Return True if IPv6 appears usable for URL's host.

    For HTTP/HTTPS URLs this performs an IPv6-only HTTP HEAD probe using
    ``curl -6`` with a short timeout. Results are cached per host for
    duration of process. This is intentionally conservative: any error or
    timeout is treated as "IPv6 not usable" for that host.
    
    HTTP Status Code Handling:
    - 2xx/3xx: IPv6 works (including redirects)
    - 401/403: IPv6 works but needs auth/specific path
    - 404: IPv6 works but path doesn't exist
    - 5xx: IPv6 works but server error
    - curl timeout/fail: IPv6 not reachable
    """

    if not url:
        return False

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return False

    if host in _ipv6_host_cache:
        return _ipv6_host_cache[host]

    ok = False
    scheme = (parsed.scheme or "https").lower()

    try:
        if scheme in ("http", "https"):
            # Use the new generic connectivity analysis
            info = analyze_url_connectivity(url, timeout)
            ok = info['ipv6_reachable']
        else:
            # Unknown scheme: be conservative.
            ok = False
    except Exception:
        ok = False

    _ipv6_host_cache[host] = ok
    return ok
