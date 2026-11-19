#!/usr/bin/env python3
"""Apply the local main checkout to the existing Compose project once it is idle."""

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

STATE = Path("/var/lib/orbitowl")
SERVICES = ("api", "worker")


def command(*args, env=None, capture=False):
    result = subprocess.run(args, env=env, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else None


def idle(database):
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        return conn.execute(
            "SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1"
        ).fetchone() is None


def ready(token, timeout=120, *, heartbeat=None, started_at=0):
    request = Request("http://127.0.0.1:8090/health/ready",
                      headers={"Authorization": f"Bearer {token}"})
    deadline = time.monotonic() + timeout
    while True:
        try:
            if heartbeat is None or heartbeat.stat().st_mtime > started_at:
                with urlopen(request, timeout=5) as response:
                    if json.load(response).get("status") == "ready":
                        return
        except (URLError, OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("OrbitOWL non pronto")
        time.sleep(2)


def apply(checkout):
    os.chdir(checkout)

    def git(*args):
        return command("git", "-c", f"safe.directory={checkout}", *args, capture=True)

    if git("branch", "--show-current") != "main":
        raise RuntimeError("La checkout deve essere su main.")
    revision = git("rev-parse", "HEAD")
    applied = STATE / "applied"
    failed = STATE / "failed"
    if applied.exists() and applied.read_text().strip() == revision:
        return
    if failed.exists() and failed.read_text().strip() == revision:
        return
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Modifiche locali tracciate: aggiornamento sospeso.")

    env_file = checkout / ".supernova/service.env"
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith("#"))
    values = {name: json.loads(value) for name, value in values.items()}
    database = Path(values["SUPERNOVA_DATA"]) / "jobs.sqlite3"
    token = Path(values["SUPERNOVA_TOKEN_FILE"]).read_text().strip()

    def compose(*args, images=None, capture=False):
        env = os.environ.copy()
        if images:
            for service, image in images.items():
                env[f"ORBITOWL_{service.upper()}_IMAGE"] = image
        return command("docker", "compose", "--project-name", "sinapsia-supernova",
                       "--env-file", str(env_file), "-f", "compose.supernova.yml",
                       *args, env=env, capture=capture)

    if not idle(database):
        print("OrbitOWL: aggiornamento in attesa, coda occupata.", flush=True)
        return
    try:
        ready(token, timeout=0)
    except RuntimeError:
        print("OrbitOWL: aggiornamento in attesa, servizio attuale non pronto.", flush=True)
        return

    previous = {}
    for service in SERVICES:
        container = compose("ps", "-q", service, capture=True)
        if not container:
            raise RuntimeError(f"Container esistente non trovato: {service}")
        previous[service] = command("docker", "inspect", "--format", "{{.Image}}",
                                    container, capture=True)

    candidate = f"orbitowl:{revision}"
    command("docker", "build", "-f", "Dockerfile.supernova", "-t", candidate, ".")
    if git("rev-parse", "HEAD") != revision or git("status", "--porcelain", "--untracked-files=no"):
        print("OrbitOWL: il codice è cambiato durante la build; riprovo al prossimo controllo.", flush=True)
        return
    if not idle(database):
        print("OrbitOWL: aggiornamento in attesa, coda occupata.", flush=True)
        return

    # Stop admission before checking the queue again. A submit already accepted
    # by the old API may have raced with the first check.
    compose("stop", "api")
    switching = False
    try:
        if not idle(database):
            compose("start", "api")
            print("OrbitOWL: lavoro appena accodato, aggiornamento rinviato.", flush=True)
            return
        rollback = {}
        for service, image in previous.items():
            tag = f"orbitowl-rollback:{service}"
            command("docker", "tag", image, tag)
            rollback[service] = tag
        switching = True
        compose("stop", "worker")
        started_at = time.time()
        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", *SERVICES,
                images=dict.fromkeys(SERVICES, candidate))
        ready(token, heartbeat=database.with_name("worker-heartbeat"), started_at=started_at)
    except Exception:
        if switching:
            failed.write_text(revision + "\n")
            compose("stop", "api")
            if idle(database):
                compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", *SERVICES,
                        images=rollback)
            else:
                compose("start", "api")
                print("OrbitOWL: rollback sospeso per non interrompere il lavoro accettato.", flush=True)
        else:
            compose("start", "api")
        raise

    pending = STATE / "applied.tmp"
    pending.write_text(revision + "\n")
    pending.replace(applied)
    failed.unlink(missing_ok=True)
    print(f"OrbitOWL aggiornato: {revision}", flush=True)


if __name__ == "__main__":
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "update.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        apply(Path(sys.argv[1]).resolve(strict=True))
