#!/usr/bin/env python3
"""
config.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import os
import sys
import yaml
from pathlib import Path


def load_config(config_dir: str) -> dict:
    """Load mirror configuration from config directory
    
    Loads main config and all repo definitions from repos-enabled/
    """
    try:
        # Load main config
        main_config_path = Path(config_dir) / 'mirror-dedupe.conf'
        with open(main_config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
        
        # Load repo definitions from repos-enabled/
        repos_dir = Path(config_dir) / 'repos-enabled'
        mirrors = []
        
        if repos_dir.exists() and repos_dir.is_dir():
            for repo_file in sorted(repos_dir.glob('*.conf')):
                try:
                    with open(repo_file, 'r') as f:
                        mirror = yaml.safe_load(f)
                        if mirror:
                            # Prepend repo_root to dest if it's relative
                            repo_root = config.get('repo_root', '/srv/mirror/repos')
                            dest = mirror.get('dest', '')
                            if not os.path.isabs(dest):
                                mirror['dest'] = os.path.join(repo_root, dest)
                            mirrors.append(mirror)
                except Exception as e:
                    print(f"Warning: Failed to load {repo_file}: {e}")
        
        # Apply global architecture mask, if configured
        arch_mask = config.get('architectures', '*')

        def _normalize_arch_mask(value):
            """Normalise global architectures mask.

            Returns None for no restriction, or a list of architectures.
            """
            if isinstance(value, str):
                v = value.strip()
                if v.lower() in ('*', 'all') or not v:
                    return None
                return [v]
            if isinstance(value, list):
                return value
            return None

        mask_arches = _normalize_arch_mask(arch_mask)

        if mask_arches is not None:
            for mirror in mirrors:
                repo_arches = mirror.get('architectures')
                if not repo_arches:
                    continue
                effective = [a for a in repo_arches if a in mask_arches]
                if effective:
                    mirror['architectures'] = effective
                else:
                    print(
                        f"Warning: Mirror '{mirror.get('name', '<unknown>')}' has no architectures left "
                        f"after applying global mask {mask_arches}; keeping original list {repo_arches}",
                    )

        # Add mirrors to config
        config['mirrors'] = mirrors
        
        return config
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)
