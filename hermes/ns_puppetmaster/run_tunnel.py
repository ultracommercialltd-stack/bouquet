"""Run a Cloudflare Quick Tunnel and publish its current HTTPS URL safely."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess  # nosec B404 -- fixed executable and argv; shell is never used.
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value.rstrip("/") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def wait_for_backend(url: str, timeout: int = 30) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 8871:
        raise ValueError("backend must be the fixed localhost onboarding service")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=2) as response:  # nosec B310
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("onboarding backend did not become healthy")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloudflared", default="/home/steven/bin/cloudflared")
    parser.add_argument("--backend", default="http://127.0.0.1:8871")
    parser.add_argument("--url-file", required=True)
    args = parser.parse_args()
    url_file = Path(args.url_file)
    try:
        url_file.unlink()
    except FileNotFoundError:
        pass
    wait_for_backend(args.backend)
    process = subprocess.Popen(  # nosec B603 -- argv list, no shell, operator-owned service arguments.
        [args.cloudflared, "tunnel", "--url", args.backend, "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def stop(signum, frame):
        del signum, frame
        process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    found = False
    if process.stdout is None:
        process.terminate()
        return 1
    for line in process.stdout:
        match = URL_RE.search(line)
        if match and not found:
            atomic_write(url_file, match.group(0))
            print(f"HTTPS onboarding tunnel ready: {match.group(0)}", flush=True)
            found = True
    code = process.wait()
    try:
        url_file.unlink()
    except FileNotFoundError:
        pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
