#!/bin/sh
# Wrapper for tuya_monitor.py with lock and logging (FreeBSD daemon)

LOCKFILE="$HOME/tuya_monitor.lock"
LOGFILE="$HOME/tuya_monitor.log"

# Source environment
. $HOME/tuya_monitor.txt

# Run as daemon, write stdout + stderr to log, create PID lockfile
/usr/sbin/daemon \
    -p "$LOCKFILE" \
    -o "$LOGFILE" \
    -m 3 \
    /usr/bin/env python $HOME/tuya_monitor.py

