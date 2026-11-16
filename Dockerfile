# syntax=docker/dockerfile:1
# Multi-arch image: plain Python base + Playwright Chromium + Camoufox + Xvfb.
# Built natively per architecture: linux/arm64 and linux/amd64 (the release workflow builds and
# starts it on a native runner of each; users build it with docker-compose.yml).
#
# Targets:
#   runtime  the service only: no dev dependencies, no tests       -> web-scraper-service:rc
#   dev      runtime + test dependencies; the tests run with the working tree mounted
#            (scripts/test.sh)                                       -> web-scraper-service:dev-next
#
#   scripts/dock.sh docker build --target runtime -t web-scraper-service:rc .
#   scripts/dock.sh docker build --target dev -t web-scraper-service:dev-next .

# python:3.13-slim-trixie, multi-arch index digest (2026-11)
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    HOME=/home/app \
    WSS_DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth tini ca-certificates fonts-liberation fonts-noto-color-emoji fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --home-dir /home/app --uid 10001 app \
 && mkdir -p /data /opt/ms-playwright && chown app:app /data /opt/ms-playwright

# every Python package pinned with hashes (requirements.in -> scripts/lock.sh -> requirements.lock)
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.lock \
 && python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()" \
 && python -c "from browserforge.fingerprints import FingerprintGenerator; FingerprintGenerator().generate()" \
 && rm -rf /tmp/requirements.lock /root/.cache

# Chromium: the service runs it headed on Xvfb, so the separate headless shell is not
# installed (--no-shell); its system libraries; Firefox's libraries for Camoufox.
RUN python -m playwright install --with-deps --no-shell chromium \
 && python -m playwright install-deps firefox \
 && rm -rf /var/lib/apt/lists/* /root/.cache /tmp/* \
 && chown -R app:app /opt/ms-playwright

USER app
# Camoufox: the browser release that camoufox==0.5.9 pins (browser-pin.json: official
# v158.0.1-beta.41). camoufox checks the download against the release asset's digest and
# writes it to version.json; the build also checks it against the digest pinned here (per
# architecture) and fails when the fetch fails or the digest differs: no silent
# Chromium-only image. The download archive is a temporary file camoufox deletes.
# Pinned digests: the release assets camoufox-158.0.1-beta.41-lin.arm64.zip and
# camoufox-158.0.1-beta.41-lin.x86_64.zip of github.com/daijro/camoufox, release
# v158.0.1-beta.41 (GitHub release asset digests).
ARG CAMOUFOX_VERSION=158.0.1-beta.41
ARG CAMOUFOX_SHA256_ARM64=60e25f6eb95e13d9ec655bc02913d51057e669cb31542e6ace9d2190ab5380af
ARG CAMOUFOX_SHA256_AMD64=3f2eb1d0451fa58e45e43e55f5e6181565069791ae60c0574f0ceb1f01341c33
# The service always presents Windows identities (os="windows" everywhere), so the macOS-only
# font set of the Camoufox bundle (fonts/M, ~0.8 GB) is never used; removed in the same layer.
ARG CAMOUFOX_DROP_MACOS_FONTS=1
# camoufox looks the release up through the GitHub API; an anonymous call shares the build
# host's rate limit (60 an hour), which shared CI runners exhaust. An optional build secret
# github_token (docker build --secret id=github_token,env=GITHUB_TOKEN) authenticates that
# call; it is mounted for this step only and never stored in the image. Without it the
# fetch is anonymous, which is enough for a build on one's own machine.
RUN --mount=type=secret,id=github_token,required=false,mode=0444 \
    GITHUB_TOKEN="$(cat /run/secrets/github_token 2>/dev/null || true)" python -m camoufox fetch \
 && python -c "import json,os,platform,pathlib,sys; \
v=os.environ['CAMOUFOX_VERSION']; \
dirs=sorted((pathlib.Path.home()/'.cache/camoufox/browsers/official').glob(v+'*')); \
dirs or sys.exit('camoufox '+v+' is not installed'); \
sha=json.loads((dirs[0]/'version.json').read_text()).get('sha256'); \
want=os.environ['CAMOUFOX_SHA256_ARM64'] if platform.machine() in ('aarch64','arm64') else os.environ['CAMOUFOX_SHA256_AMD64']; \
print('camoufox', v, platform.machine(), 'asset sha256', sha); \
want and sha!=want and sys.exit('camoufox asset sha256 differs from the pinned '+want)" \
 && if [ "$CAMOUFOX_DROP_MACOS_FONTS" = "1" ]; then rm -rf "$HOME"/.cache/camoufox/browsers/official/*/fonts/M; fi \
 && rm -rf "$HOME/.cache/pip" /tmp/*

WORKDIR /srv
COPY --chown=app:app contract /srv/contract
COPY --chown=app:app app /srv/app
COPY --chown=app:app scripts/entrypoint.sh /srv/scripts/entrypoint.sh
# the protection pass-rate report: docker exec <container> python /srv/scripts/protection_report.py …
COPY --chown=app:app scripts/protection_report.py /srv/scripts/protection_report.py
COPY --chown=app:app NOTICE LICENSE /srv/

# Image metadata (OCI). The release workflow passes the version and the commit; the commit is
# also WSS_BUILD, which GET /health reports as service.build. A local build without them gets
# version "dev" and no build.
ARG WSS_VERSION=dev
ARG WSS_REVISION=
LABEL org.opencontainers.image.title="web-scraper-service" \
      org.opencontainers.image.description="Scraper service of the Flow Steward Web Scraper extension" \
      org.opencontainers.image.source="https://github.com/Flow-Steward/web-scraper-service" \
      org.opencontainers.image.url="https://github.com/Flow-Steward/web-scraper-service" \
      org.opencontainers.image.version="${WSS_VERSION}" \
      org.opencontainers.image.revision="${WSS_REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Flow Steward"
ENV WSS_BUILD=${WSS_REVISION}

EXPOSE 8080
VOLUME ["/data"]
# tini reaps the browsers' children; the entrypoint starts Xvfb (browsers run headed on it)
ENTRYPOINT ["/usr/bin/tini", "--", "/srv/scripts/entrypoint.sh"]
CMD ["python", "-m", "app"]


FROM runtime AS dev
USER root
COPY requirements-dev.lock /tmp/requirements-dev.lock
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements-dev.lock \
 && rm -rf /tmp/requirements-dev.lock /root/.cache
COPY --chown=app:app pytest.ini /srv/
USER app
# the tests run with the working tree mounted at /srv (scripts/test.sh); tests/ is not baked in
