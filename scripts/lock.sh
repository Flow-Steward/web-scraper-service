#!/usr/bin/env bash
# Regenerates requirements.lock and requirements-dev.lock (every package pinned, with the
# hashes of every file PyPI publishes for it, for linux/arm64 and linux/amd64 alike) inside a
# throwaway container (never on the host).
set -eu
cd "$(dirname "$0")/.."
exec scripts/dock.sh docker run --rm -v "$PWD":/w -w /w python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 sh -c '
  pip install -q --no-cache-dir uv==0.9.9 &&
  uv pip compile --quiet --universal --python-version 3.13 --generate-hashes --no-header --no-strip-extras -o requirements.lock requirements.in &&
  uv pip compile --quiet --universal --python-version 3.13 --generate-hashes --no-header -o requirements-dev.lock requirements-dev.in'
