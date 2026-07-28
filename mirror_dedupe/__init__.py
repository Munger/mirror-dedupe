## @file __init__.py
##
## @brief Mirror synchronisation with global deduplication.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import os
import re


def _get_version():
    changelog = os.path.join(os.path.dirname(__file__), '..', 'debian', 'changelog')
    if os.path.exists(changelog):
        with open(changelog) as f:
            m = re.match(r'^mirror-dedupe \(([^-]+)-\d+\)', f.readline())
            if m:
                return m.group(1)
    try:
        from importlib.metadata import version as _v
        return _v('mirror-dedupe')
    except Exception:
        return '0.0.0'


__version__ = _get_version()
