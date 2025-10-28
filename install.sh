#!/bin/bash
#
# install.sh : Ubuntu mirror synchronisation with global deduplication
#
# Copyright (c) 2025 Tim Hosking
# Email: tim@mungerware.com
# Website: https://github.com/munger
# Licence: MIT
#

#
# install.sh - Install mirror-dedupe with systemd integration
#
# Usage:
#   sudo ./install.sh          # Install from current directory
#   sudo ./install.sh --pip    # Install from PyPI
#

set -e

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root (use sudo)"
    exit 1
fi

# Determine installation method
if [ "$1" = "--pip" ]; then
    echo "Installing mirror-dedupe from PyPI..."
    pip install mirror-dedupe
    INSTALL_DIR=$(pip show mirror-dedupe | grep Location | cut -d' ' -f2)
    PACKAGE_DIR="$INSTALL_DIR/mirror_dedupe"
else
    echo "Installing mirror-dedupe from current directory..."
    pip install .
    PACKAGE_DIR="$(pwd)"
fi

echo ""
echo "Installing systemd service files..."
install -D -m 644 systemd/mirror-dedupe.service /usr/lib/systemd/system/mirror-dedupe.service
install -D -m 644 systemd/mirror-dedupe.timer /usr/lib/systemd/system/mirror-dedupe.timer

echo "Installing man page..."
install -D -m 644 debian/mirror-dedupe.1 /usr/share/man/man1/mirror-dedupe.1
mandb -q 2>/dev/null || true

echo "Installing configuration files..."
mkdir -p /etc/mirror-dedupe/repos.d
if [ ! -f /etc/mirror-dedupe/mirror-dedupe.conf ]; then
    install -D -m 644 config/mirror-dedupe.conf /etc/mirror-dedupe/mirror-dedupe.conf
    echo "  Installed: /etc/mirror-dedupe/mirror-dedupe.conf"
else
    echo "  Skipped: /etc/mirror-dedupe/mirror-dedupe.conf (already exists)"
fi

# Install example configs
for example in config/repos.d/*.example; do
    if [ -f "$example" ]; then
        install -D -m 644 "$example" /etc/mirror-dedupe/repos.d/$(basename "$example")
    fi
done

echo ""
echo "Reloading systemd daemon..."
systemctl daemon-reload

echo ""
echo "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Configure your mirrors in /etc/mirror-dedupe/repos.d/"
echo "  2. Enable the timer: systemctl enable --now mirror-dedupe.timer"
echo "  3. Check status: systemctl status mirror-dedupe.timer"
echo "  4. View logs: journalctl -u mirror-dedupe.service"
echo ""
echo "To run manually: mirror-dedupe --help"
