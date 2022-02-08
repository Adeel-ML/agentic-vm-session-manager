#!/bin/bash

set -e

export DISPLAY=:${DISPLAY_NUM}
./xvfb_startup.sh

# Disable X audible bell to prevent noVNC ding sounds during agent actions.
if command -v xset >/dev/null 2>&1; then
	xset b off >/dev/null 2>&1 || true
fi

./tint2_startup.sh
./mutter_startup.sh
./x11vnc_startup.sh
