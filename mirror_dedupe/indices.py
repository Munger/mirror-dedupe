#!/usr/bin/env python3
"""
indices.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import os
import gzip
from typing import Dict, Set


def read_gzipped_file(filepath: str) -> str:
    """Read and decompress a gzipped file from local filesystem"""
    try:
        with gzip.open(filepath, 'rt', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"  Error reading {filepath}: {e}")
        return None


def parse_packages_file(content: str) -> Dict[str, Dict[str, str]]:
    """
    Parse Packages file and extract package info.
    Returns: {filename: {'sha256': hash, 'size': size, 'package': name}}
    """
    packages = {}
    current_package = {}
    
    for line in content.split('\n'):
        line = line.rstrip()
        
        if not line:
            if 'filename' in current_package and 'sha256' in current_package:
                filename = current_package['filename']
                packages[filename] = {
                    'sha256': current_package['sha256'],
                    'size': current_package.get('size', '0'),
                    'package': current_package.get('package', 'unknown')
                }
            current_package = {}
            continue
        
        if ':' in line and not line.startswith(' '):
            key, value = line.split(':', 1)
            key = key.strip().lower()
            value = value.strip()
            
            if key == 'package':
                current_package['package'] = value
            elif key == 'filename':
                current_package['filename'] = value
            elif key == 'sha256':
                current_package['sha256'] = value
            elif key == 'size':
                current_package['size'] = value
    
    return packages


def parse_sources_file(content: str) -> Dict[str, Dict[str, str]]:
    """
    Parse Sources file and extract source package info.
    Returns: {filename: {'sha256': hash, 'size': size, 'package': name}}
    """
    sources = {}
    current_package = {}
    current_files = []
    
    for line in content.split('\n'):
        line = line.rstrip()
        
        if not line:
            if 'directory' in current_package and current_files:
                directory = current_package['directory']
                for filename, size, sha256 in current_files:
                    full_path = f"{directory}/{filename}"
                    sources[full_path] = {
                        'sha256': sha256,
                        'size': size,
                        'package': current_package.get('package', 'unknown')
                    }
            current_package = {}
            current_files = []
            continue
        
        if ':' in line and not line.startswith(' '):
            key, value = line.split(':', 1)
            key = key.strip().lower()
            value = value.strip()
            
            if key == 'package':
                current_package['package'] = value
                current_package['in_checksums'] = False
            elif key == 'directory':
                current_package['directory'] = value
                current_package['in_checksums'] = False
            elif key == 'checksums-sha256':
                current_package['in_checksums'] = True
            else:
                current_package['in_checksums'] = False
        elif line.startswith(' ') and current_package.get('in_checksums'):
            parts = line.strip().split()
            if len(parts) >= 3:
                sha256 = parts[0]
                size = parts[1]
                filename = ' '.join(parts[2:])
                if filename and not filename.endswith(')') and '(' not in filename:
                    current_files.append((filename, size, sha256))
    
    if 'directory' in current_package and current_files:
        directory = current_package['directory']
        for filename, size, sha256 in current_files:
            full_path = f"{directory}/{filename}"
            sources[full_path] = {
                'sha256': sha256,
                'size': size,
                'package': current_package.get('package', 'unknown')
            }
    
    return sources


def parse_release_file(dest_base: str, distribution: str) -> Set[str]:
    """
    Parse Release file and return set of available index files.
    Returns: set of relative paths like 'main/binary-amd64/Packages.gz'
    """
    release_path = f"{dest_base}/dists/{distribution}/Release"
    
    if not os.path.exists(release_path):
        return set()
    
    available_files = set()
    in_sha256_section = False
    
    try:
        with open(release_path, 'r') as f:
            for line in f:
                line = line.rstrip()
                
                # Look for SHA256 section
                if line.startswith('SHA256:'):
                    in_sha256_section = True
                    continue
                elif line and not line.startswith(' '):
                    # New section started
                    in_sha256_section = False
                
                # Parse file entries in SHA256 section
                if in_sha256_section and line.startswith(' '):
                    parts = line.split()
                    if len(parts) >= 3:
                        # Format: " <hash> <size> <path>"
                        file_path = parts[2]
                        available_files.add(file_path)
    except Exception as e:
        print(f"  Warning: Could not parse Release file: {e}")
        return set()
    
    return available_files


def get_packages_index(dest_base: str, distribution: str, component: str, arch: str) -> Dict[str, Dict[str, str]]:
    """Read and parse local Packages.gz for a specific component/arch"""
    packages_path = f"{dest_base}/dists/{distribution}/{component}/binary-{arch}/Packages.gz"
    content = read_gzipped_file(packages_path)
    
    if content is None:
        return {}
    
    return parse_packages_file(content)


def get_sources_index(dest_base: str, distribution: str, component: str) -> Dict[str, Dict[str, str]]:
    """Read and parse local Sources.gz for a specific component"""
    sources_path = f"{dest_base}/dists/{distribution}/{component}/source/Sources.gz"
    content = read_gzipped_file(sources_path)
    
    if content is None:
        return {}
    
    return parse_sources_file(content)
