#!/usr/bin/env python3
"""
setup.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

from setuptools import setup, find_packages
import os
import re

# Read long description from README
with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

# Extract version from debian/changelog
def get_version():
    with open("debian/changelog", "r") as f:
        first_line = f.readline()
        match = re.match(r'^mirror-dedupe \(([^-]+)-\d+\)', first_line)
        if match:
            return match.group(1)
    return "0.0.0"

setup(
    name="mirror-dedupe",
    version=get_version(),
    description="Ubuntu mirror synchronisation with global deduplication",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Tim Hosking",
    author_email="tim@mungerware.com",
    url="https://github.com/munger/mirror-dedupe",
    project_urls={
        "Bug Tracker": "https://github.com/munger/mirror-dedupe/issues",
        "Source Code": "https://github.com/munger/mirror-dedupe",
    },
    packages=find_packages(),
    install_requires=[
        "PyYAML>=6.0",
    ],
    entry_points={
        'console_scripts': [
            'mirror-dedupe=mirror_dedupe.cli:main',
            'mirror-dedupe-scan=mirror_dedupe.scan:main',
        ],
    },
    python_requires=">=3.8",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: System :: Archiving :: Mirroring",
        "Topic :: System :: Systems Administration",
    ],
    keywords="mirror, deduplication, hardlink, ubuntu, repository, apt",
    license="MIT",
)
