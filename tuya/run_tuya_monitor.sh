#!/bin/sh
# Wrapper for tuya_monitor.py with lock and logging (FreeBSD daemon)
# Works relative to script location, not $HOME

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

LOCKFILE="$SCRIPT_DIR/tuya_monitor.lock"
LOGFILE="$SCRIPT_DIR/tuya_monitor.log"

# Source environment from same directory
. "$SCRIPT_DIR/tuya_monitor.txt"

# Run as daemon, write stdout + stderr to log, create PID lockfile
/usr/sbin/daemon \
    -p "$LOCKFILE" \
    -o "$LOGFILE" \
    -m 3 \
    /usr/bin/env python "$SCRIPT_DIR/tuya_monitor.py"

