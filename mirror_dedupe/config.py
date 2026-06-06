#!/usr/bin/env python3
## @file config.py
##
## @brief Configuration loader for mirror-dedupe.
##
## Provides the ``Config`` singleton-style class that loads
## ``mirror-dedupe.conf`` and discovers enabled mirrors from
## ``repos-enabled/*.conf``.  Exposes settings via both named attributes
## and dict-style access.
##
## @copyright Copyright (c) 2025-2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import os
import sys
import yaml
from pathlib import Path
from typing import Dict, List

from mirror_dedupe.lib.log import log, setup_log_colours


DEFAULT_CONFIG_DIR = "/etc/mirror-dedupe"


class Config:
    ## @brief Singleton-style loader for global config with attribute access.

    _instance: "Config | None" = None
    _config_dir: str | None = None

    @classmethod
    def load(cls, config_dir: str = DEFAULT_CONFIG_DIR) -> "Config":
        ## @brief Load (or return cached) Config for *config_dir*.
        ##
        ## Returns the cached instance if one exists for any loaded
        ## directory when called without an explicit override (i.e. the
        ## default).  When an explicit *config_dir* is given, reloads if
        ## different from the cached directory.
        ##
        ## @param config_dir  Path to the configuration directory.
        ## @return A Config instance.

        config_dir_resolved = str(Path(config_dir or DEFAULT_CONFIG_DIR).resolve())
        if cls._instance is not None:
            if config_dir is None or config_dir == DEFAULT_CONFIG_DIR:
                return cls._instance
            if cls._config_dir == config_dir_resolved:
                return cls._instance
        cls._instance = cls(config_dir_resolved)
        cls._config_dir = config_dir_resolved
        return cls._instance

    def __init__(self, config_dir_resolved: str) -> None:
        ## @brief Initialise Config from a resolved directory path.
        ##
        ## Loads ``mirror-dedupe.conf`` from *config_dir_resolved* and
        ## extracts global settings (``disable_ipv6``, ``repo_root``,
        ## ``pool_root``, ``architectures``, ``collapse_distributions``,
        ## ``buffer_size``, ``parallel_downloads``, ``curl_timeout``,
        ## ``max_retries``, ``progress_interval``, ``no_hardlinks``) into
        ## named attributes on the instance.
        ##
        ## Iterates ``repos-enabled/*.conf`` for mirror definitions,
        ## resolves relative ``dest`` paths against ``repo_root``, and
        ## applies per-mirror ``disable_ipv6`` inheritance.  Applies a
        ## global architecture mask to each mirror's architecture list;
        ## mirrors with no surviving architectures after the mask are
        ## warned but keep their original list.
        ##
        ## Finally synchronises all attributes back into ``self._data`` so
        ## that both attribute and dict-style access return identical
        ## values.
        ##
        ## @param config_dir_resolved  Absolute path to the configuration
        ##                             directory.

        self._config_dir = config_dir_resolved
        main_config_path = Path(config_dir_resolved) / 'mirror-dedupe.conf'
        try:
            with open(main_config_path, 'r') as f:
                self._data = yaml.safe_load(f) or {}
        except Exception as e:
            log(f"Error loading configuration from {config_dir_resolved}: {e}", level="ERROR")
            sys.exit(1)

        global_disable_ipv6 = self._data.get('disable_ipv6', False)
        self.disable_ipv6 = global_disable_ipv6

        self.repo_root = self._data.get('repo_root', '/srv/mirror/repos')
        self.pool_root = self._data.get('pool_root', '/srv/mirror/pool')

        self.architectures = self._data.get('architectures', '*')
        self.collapse_distributions = self._data.get('collapse_distributions', False)
        self.buffer_size = self._data.get('buffer_size', 1048576)
        self.parallel_downloads = self._data.get('parallel_downloads', 10)
        self.max_concurrent_syncs = self._data.get('max_concurrent_syncs', 2)
        self.curl_timeout = self._data.get('curl_timeout', 900)
        self.max_retries = self._data.get('max_retries', 3)
        self.progress_interval = self._data.get('progress_interval', 1000)
        self.no_hardlinks = bool(self._data.get('no_hardlinks', False))
        self.sweep_pool_after_sync = bool(self._data.get('sweep_pool_after_sync', False))
        raw_additional = self._data.get('additional_repos', []) or []
        self.additional_repos: Dict[str, str] = {}
        for entry in raw_additional:
            if isinstance(entry, dict):
                aname = entry.get("name", "")
                adest = entry.get("dest", "")
                if aname and adest:
                    if not os.path.isabs(adest):
                        adest = os.path.join(self.repo_root, adest)
                    self.additional_repos[aname] = adest
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
                            # Resolve relative dest paths against repo_root
                            if not os.path.isabs(dest):
                                mirror['dest'] = os.path.join(repo_root, dest)
                            mirror['disable_ipv6'] = mirror.get('disable_ipv6', global_disable_ipv6)
                            mirrors.append(mirror)
                except Exception as e:
                    log(f"Warning: Failed to load {repo_file}: {e}", level="WARN")

        # Global arch mask: only keep architectures present in both mask and per-mirror list
        arch_mask = self._data.get('architectures', '*')

        def _normalize_arch_mask(value):
            ## @brief Normalise an architecture mask value.
            ##
            ## ``"*"``, ``"all"``, or empty → ``None`` (passthrough).
            ## A single string → one-element list.  A list → itself.
            ## Anything else → ``None``.
            ##
            ## @param value  Raw architecture mask from config.
            ## @return Normalised mask or ``None``.
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
                    # Keep original list rather than silently emptying it
                    log(
                        f"Warning: Mirror '{mirror.get('name', '<unknown>')}' has no architectures left "
                        f"after applying global mask {mask_arches}; keeping original list {repo_arches}",
                        level="WARN",
                    )

        self.mirrors = mirrors
        self._data['disable_ipv6'] = self.disable_ipv6
        self._data['repo_root'] = self.repo_root
        self._data['pool_root'] = self.pool_root
        self._data['architectures'] = self.architectures
        self._data['collapse_distributions'] = self.collapse_distributions
        self._data['buffer_size'] = self.buffer_size
        self._data['parallel_downloads'] = self.parallel_downloads
        self._data['max_concurrent_syncs'] = self.max_concurrent_syncs
        self._data['curl_timeout'] = self.curl_timeout
        self._data['max_retries'] = self.max_retries
        self._data['progress_interval'] = self.progress_interval
        self._data['no_hardlinks'] = self.no_hardlinks
        self._data['sweep_pool_after_sync'] = self.sweep_pool_after_sync
        self._data['mirrors'] = self.mirrors
        self._data['additional_repos'] = self.additional_repos
        self._data['log_colour'] = self._data.get('log_colour', 'DEFAULT')
        self._data['log_colour_bg'] = self._data.get('log_colour_bg', 'NONE')

        setup_log_colours(
            self._data['log_colour'],
            self._data['log_colour_bg'],
            mirrors,
        )

    def __getitem__(self, key):
        ## @brief Dict-style access to config values.
        ##
        ## Delegates to ``self._data.__getitem__`` so that ``cfg["key"]``
        ## behaves identically to ``cfg._data["key"]``.
        ##
        ## @param key  Config key to look up.
        ## @return The value for *key*.
        ## @raises KeyError  If *key* is not present.

        return self._data[key]

    def get(self, key, default=None):
        ## @brief Dict-style get with a default fallback.
        ##
        ## @param key      Config key to look up.
        ## @param default  Fallback value (default ``None``).
        ## @return The value for *key*, or *default* if not present.
        return self._data.get(key, default)

    def resolve_dest(self, name: str) -> str | None:
        ## @brief Resolve a repo *name* to its absolute ``dest`` path.
        ##
        ## Checks in order:
        ## 1. ``repos-enabled/{name}.conf`` → ``dest`` field (if absolute already)
        ## 2. ``repos-available/{name}.conf`` → ``dest`` field
        ## 3. ``additional_repos`` prefs
        ## 4. Fallback: ``{repo_root}/{name}``
        ##
        ## @param name  Repo name to resolve.
        ## @return The absolute dest path, or ``None`` if unresolvable.

        # Check repos-enabled first
        for repos_dir_rel in ("repos-enabled", "repos-available"):
            d = Path(self._config_dir) / repos_dir_rel / f"{name}.conf"  # type: ignore[arg-type]
            if d.exists():
                try:
                    with open(d) as f:
                        cfg = yaml.safe_load(f) or {}
                    dest = cfg.get("dest")
                    if dest:
                        return dest if os.path.isabs(dest) else os.path.join(self.repo_root, dest)
                except Exception:
                    pass

        # Check additional_repos
        if name in self.additional_repos:
            return self.additional_repos[name]

        # Fallback: treat name as a subdirectory of repo_root
        fallback = os.path.join(self.repo_root, name)
        if os.path.isdir(fallback):
            return fallback

        return None

    def list_repo_names(self) -> List[str]:
        ## @brief Return sorted list of all known repo names.
        ##
        ## Combines entries from ``repos-available/*.conf``,
        ## ``repos-enabled/*.conf`` (by filename stem) and the
        ## ``additional_repos`` section in ``mirror-dedupe.conf``.
        ##
        ## @return Sorted list of repo names.

        names: set[str] = set()

        # Repos-available config files
        repa = Path(self._config_dir) / "repos-available"
        if repa.exists():
            for f in repa.glob("*.conf"):
                names.add(f.stem)

        # Repos-enabled config files
        repd = Path(self._config_dir) / "repos-enabled"
        if repd.exists():
            for f in repd.glob("*.conf"):
                names.add(f.stem)

        # Additional repos from prefs
        names.update(self.additional_repos.keys())

        return sorted(names)
