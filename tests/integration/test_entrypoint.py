"""A restarted container keeps /tmp with the previous run's Xvfb socket and lock;
the entrypoint must start Xvfb unless one really runs on the display."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys

import pytest

from tests.conftest import ROOT

DISPLAY = ":97"  # not the test container's own :99
LOCK = "/tmp/.X97-lock"
SOCK = "/tmp/.X11-unix/X97"
LAUNCH = ("import asyncio\nfrom playwright.async_api import async_playwright\n"
          "async def m():\n  async with async_playwright() as p:\n"
          "    b = await p.chromium.launch(headless=False, chromium_sandbox=False, args=['--no-sandbox'])\n    await b.close()\n"
          "asyncio.run(m()); print('launched')")


def _lock_pid() -> int:
    with open(LOCK) as fh:
        return int(fh.read().strip())


def _entry(*cmd: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DISPLAY=DISPLAY)
    return subprocess.run([str(ROOT / "scripts" / "entrypoint.sh"), *cmd], env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def stale_display():
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    for path in (SOCK, LOCK):
        if os.path.exists(path):
            os.remove(path)
    s = socket.socket(socket.AF_UNIX)
    s.bind(SOCK)  # a socket file nobody listens on, as after a restart
    s.close()
    yield
    try:
        os.kill(_lock_pid(), signal.SIGTERM)
    except (OSError, ValueError):
        pass


@pytest.mark.parametrize("stale_pid", ["4190000", str(os.getpid())], ids=["dead-pid", "pid-of-another-process"])
def test_stale_socket_and_lock_do_not_stop_xvfb(stale_display, stale_pid):
    with open(LOCK, "w") as fh:
        fh.write(f"{stale_pid:>10}\n")
    first = _entry(sys.executable, "-c", LAUNCH)
    assert first.returncode == 0 and "launched" in first.stdout, first.stderr[-2000:]
    pid = _lock_pid()
    assert str(pid) != stale_pid and open(f"/proc/{pid}/comm").read().strip() == "Xvfb"
    # a live Xvfb is kept: a second start reuses it
    second = _entry(sys.executable, "-c", LAUNCH)
    assert second.returncode == 0 and "launched" in second.stdout, second.stderr[-2000:]
    assert _lock_pid() == pid
