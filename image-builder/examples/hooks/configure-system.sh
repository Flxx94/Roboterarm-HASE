#!/usr/bin/env bash
set -euo pipefail

# Hooks execute in the builder container, not inside the target chroot.
# ROOTFS points to the mounted Raspberry Pi OS root filesystem.
install -d "$ROOTFS/etc/robot-arm"
