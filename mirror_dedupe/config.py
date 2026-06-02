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


DEFAULT_CONFIG_DIR = "/etc/mirror-dedupe"


class Config:
    """Singleton-style loader for global config with attribute access."""

    _instance: "Config | None" = None
    _config_dir: str | None = None

    @classmethod
    def load(cls, config_dir: str = DEFAULT_CONFIG_DIR) -> "Config":
        config_dir_resolved = str(Path(config_dir or DEFAULT_CONFIG_DIR).resolve())
        if cls._instance is not None and cls._config_dir == config_dir_resolved:
            return cls._instance
        cls._instance = cls(config_dir_resolved)
        cls._config_dir = config_dir_resolved
        return cls._instance

    def __init__(self, config_dir_resolved: str) -> None:
        # Load main config
        main_config_path = Path(config_dir_resolved) / 'mirror-dedupe.conf'
        try:
            with open(main_config_path, 'r') as f:
                self._data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Error loading configuration from {config_dir_resolved}: {e}")
            sys.exit(1)

        # Global IPv6 control
        global_disable_ipv6 = self._data.get('disable_ipv6', False)
        self.disable_ipv6 = global_disable_ipv6

        # Global roots
        self.repo_root = self._data.get('repo_root', '/srv/mirror/repos')
        self.pool_root = self._data.get('pool_root', '/srv/mirror/pool')

        # Optional tuning parameters
        self.architectures = self._data.get('architectures', '*')
        self.collapse_distributions = self._data.get('collapse_distributions', False)
        self.buffer_size = self._data.get('buffer_size', 1048576)
        self.parallel_downloads = self._data.get('parallel_downloads', 10)
        self.curl_timeout = self._data.get('curl_timeout', 900)
        self.max_retries = self._data.get('max_retries', 3)
        self.progress_interval = self._data.get('progress_interval', 1000)
        self.no_hardlinks = bool(self._data.get('no_hardlinks', False))

        # Load repo definitions from repos-enabled/
        repos_dir = Path(config_dir_resolved) / 'repos-enabled'
        mirrors: list[dict] = []

        if repos_dir.exists() and repos_dir.is_dir():
            for repo_file in sorted(repos_dir.glob('*.conf')):
                try:
                    with open(repo_file, 'r') as f:
                        mirror = yaml.safe_load(f)
                        if mirror:
                            repo_root = self.repo_root
                            dest = mirror.get('dest', '')
                            if not os.path.isabs(dest):
                                mirror['dest'] = os.path.join(repo_root, dest)
                            mirror['disable_ipv6'] = mirror.get('disable_ipv6', global_disable_ipv6)
                            mirrors.append(mirror)
                except Exception as e:
                    print(f"Warning: Failed to load {repo_file}: {e}")

        # Apply global architecture mask, if configured
        arch_mask = self._data.get('architectures', '*')

        def _normalize_arch_mask(value):
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

        self.mirrors = mirrors
        # keep the raw data for legacy item access
        self._data['disable_ipv6'] = self.disable_ipv6
        self._data['repo_root'] = self.repo_root
        self._data['pool_root'] = self.pool_root
        self._data['architectures'] = self.architectures
        self._data['collapse_distributions'] = self.collapse_distributions
        self._data['buffer_size'] = self.buffer_size
        self._data['parallel_downloads'] = self.parallel_downloads
        self._data['curl_timeout'] = self.curl_timeout
        self._data['max_retries'] = self.max_retries
        self._data['progress_interval'] = self.progress_interval
        self._data['no_hardlinks'] = self.no_hardlinks
        self._data['mirrors'] = self.mirrors

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)


