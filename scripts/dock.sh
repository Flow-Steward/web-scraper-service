#!/usr/bin/env bash
# Runs a docker command, optionally under a shared lock directory. Set WSS_DOCKER_LOCK to a
# path (e.g. ~/.web-scraper-docker.lock) to serialize Docker work between processes on one
# machine; without it, or where the lock's parent directory does not exist, the command
# runs without a lock.
# Usage: scripts/dock.sh <command...>
set -u
LOCK="${WSS_DOCKER_LOCK:-}"
[ -n "$LOCK" ] || exec "$@"
[ -d "$(dirname "$LOCK")" ] || exec "$@"
OWNER="service-developer (web-scraper-service)"
waited=0
while ! mkdir "$LOCK" 2>/dev/null; do
  if [ $waited -ge 3600 ]; then echo "lock wait timeout" >&2; exit 99; fi
  echo "docker lock held by: $(cat "$LOCK/owner" 2>/dev/null)" >&2
  sleep 30; waited=$((waited+30))
done
echo "$OWNER $(date -u +%FT%TZ)" > "$LOCK/owner"
cleanup() { rm -rf "$LOCK"; }
trap cleanup EXIT INT TERM
"$@"
