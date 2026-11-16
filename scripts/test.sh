#!/usr/bin/env bash
# Runs pytest inside the dev image with the working tree mounted (never on the host).
# Usage: scripts/test.sh [pytest args...]
# WSS_TEST_IMAGE: the dev image (default web-scraper-service:dev-next); WSS_TEST_CPUS: CPUs for
# the container (default 4; a CI runner with fewer CPUs sets its own count).
set -u
cd "$(dirname "$0")/.."
exec scripts/dock.sh docker run --rm --name web-scraper-dev-test --memory 3g --cpus "${WSS_TEST_CPUS:-4}" --shm-size 1g \
  --add-host shop.test:127.0.0.1 --add-host www.shop.test:127.0.0.1 --add-host plain.test:127.0.0.1 \
  --add-host guard.test:127.0.0.1 --add-host repair.test:127.0.0.1 --add-host parts.test:127.0.0.1 --add-host fail.test:127.0.0.1 --add-host restart.test:127.0.0.1 --add-host hook.test:127.0.0.1 --add-host solver.test:127.0.0.1 --add-host internal.test:127.0.0.1 --add-host repair2.test:127.0.0.1 --add-host queue.test:127.0.0.1 --add-host nold.test:127.0.0.1 --add-host listings.test:127.0.0.1 --add-host mixed.test:127.0.0.1 --add-host bigmap.test:127.0.0.1 --add-host variants.test:127.0.0.1 --add-host scope.test:127.0.0.1 --add-host oldmap.test:127.0.0.1 --add-host week.test:127.0.0.1 --add-host links.test:127.0.0.1 \
  -e PYTHONDONTWRITEBYTECODE=1 -e DISPLAY=:99 \
  -v "$PWD":/srv:ro --tmpfs /tmp:rw,size=1g,uid=10001 \
  --entrypoint /srv/scripts/entrypoint.sh "${WSS_TEST_IMAGE:-web-scraper-service:dev-next}" \
  python -m pytest -p no:cacheprovider "$@"
