#!/usr/bin/env python3
"""Create private local settings for a fresh OrbitOWL installation."""

import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def write_new(path, content):
    """Keep existing configuration and credentials on subsequent invocations."""
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
            out.write(content)
    except FileExistsError:
        return


def initialize(root, runner_url, port):
    url = urlsplit(runner_url)
    if (
        url.scheme not in {"http", "https"} or not url.hostname
        or url.username or url.password or url.query or url.fragment
        or any(c.isspace() or c in "'\"#$\\" for c in runner_url)
    ):
        raise ValueError("Use a plain HTTP(S) runner URL without credentials.")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    state = root / ".orbitowl"
    state.mkdir(mode=0o700, exist_ok=True)
    (state / "data").mkdir(mode=0o700, exist_ok=True)
    write_new(state / "api-token", secrets.token_urlsafe(48) + "\n")
    settings = {
        "ORBITOWL_UID": os.getuid(),
        "ORBITOWL_GID": os.getgid(),
        "ORBITOWL_PORT": port,
        "ORBITOWL_RUNNER_URL": runner_url,
        "ORBITOWL_CONTEXT": 65536,
        "ORBITOWL_MODEL": "qwen3.8:27b",
        "ORBITOWL_DATA_DIR": "./.orbitowl/data",
        "ORBITOWL_TOKEN_FILE": "./.orbitowl/api-token",
    }
    write_new(root / ".env", "".join(f"{key}={value}\n" for key, value in settings.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-url", default="http://127.0.0.1:18081")
    parser.add_argument("--port", type=int, default=8110)
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("Run as the account that owns the checkout, without sudo.")
    try:
        initialize(Path(__file__).resolve().parents[1], args.runner_url, args.port)
    except ValueError as exc:
        parser.error(str(exc))
    print("OrbitOWL: configurazione in .env; token in .orbitowl/api-token.")


if __name__ == "__main__":
    main()
