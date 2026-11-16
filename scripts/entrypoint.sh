#!/bin/sh
# Starts a virtual display (the browsers run headed on it), then the command.
#
# A restarted container keeps its /tmp: the socket /tmp/.X11-unix/X<n> and the lock
# /tmp/.X<n>-lock of the previous run's Xvfb are still there although no Xvfb runs any more
# (every headed browser launch then failed). Xvfb counts as running only when
# the PID in the lock file is a live Xvfb process; otherwise the stale socket and lock are
# removed and Xvfb is started.
if [ -z "${DISPLAY:-}" ]; then export DISPLAY=:99; fi
n="${DISPLAY#:}"
n="${n%%.*}"
lock="/tmp/.X${n}-lock"
sock="/tmp/.X11-unix/X${n}"

xvfb_alive() {
  [ -e "$lock" ] || return 1
  pid=$(tr -d ' \n' < "$lock" 2>/dev/null)
  [ -n "$pid" ] || return 1
  [ -r "/proc/$pid/comm" ] || return 1
  [ "$(cat "/proc/$pid/comm" 2>/dev/null)" = "Xvfb" ]
}

if ! xvfb_alive; then
  rm -f "$sock" "$lock" 2>/dev/null
  Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
  for i in $(seq 1 50); do [ -e "$sock" ] && break; sleep 0.1; done
fi
exec "$@"
