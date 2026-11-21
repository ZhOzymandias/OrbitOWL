"""Request a Trooper.AI freeze when one OrbitOWL job completes or fails."""

import argparse
import json
import time
from subprocess import run
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from orbitowl.contracts import ServiceError, identifier
from scripts.client import NoRedirect

TROOPER_API = "https://james.trooper.ai"
TERMINAL = {"completed", "failed"}
ACTIVE = {"queued", "running"}
POLL_SECONDS = 15


def request_json(url, token):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=15) as response:
        return json.load(response)


def trooper_request(endpoint, token, payload=None):
    # Keep credentials on stdin and use the same curl client as Trooper.AI's examples.
    if any(char.isspace() for char in token):
        raise ValueError("La chiave Trooper.AI contiene spazi o ritorni a capo.")
    command = ["curl", "--disable", "--silent", "--max-time", "15", "--header", "@-",
               "--write-out", "\n%{http_code}"]
    if payload is not None:
        command += ["--header", "Content-Type: application/json", "--data-binary", json.dumps(payload)]
    try:
        result = run([*command, TROOPER_API + endpoint], input=f"Authorization: Bearer {token}\n",
                     capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise ValueError("Installa curl su Pulsar per accedere a Trooper.AI.") from exc
    if result.returncode:
        raise ValueError(f"Chiamata a Trooper.AI interrotta (curl {result.returncode}).")

    body, _, status = result.stdout.rpartition("\n")
    try:
        response = json.loads(body)
    except ValueError:
        response = None
    if not status.startswith("2"):
        detail = None
        if isinstance(response, dict):
            detail = response.get("error") or response.get("detail") or response.get("message")
        if isinstance(detail, str):
            detail = " ".join(detail.replace(token, "[omessa]").split())[:300]
        hint = f" — {detail}" if detail else ""
        raise ValueError(f"Trooper.AI ha risposto HTTP {status}{hint}")
    if response is None:
        raise ValueError("Trooper.AI non ha restituito una risposta JSON valida.")
    return response


def read_key(path):
    token = path.read_text().strip()
    if not token:
        raise ValueError(f"File del token vuoto: {path}")
    return token


def watch_job(url, job_token, job_id, server_id, trooper_key):
    last_status = None
    while True:
        try:
            job = request_json(f"{url.rstrip('/')}/v1/ontology-jobs/{job_id}", job_token)
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            print(f"Stato OrbitOWL temporaneamente indisponibile (HTTP {exc.code}).", flush=True)
        except (URLError, TimeoutError):
            print("OrbitOWL temporaneamente irraggiungibile.", flush=True)
        else:
            if not isinstance(job, dict) or job.get("id") != job_id:
                raise ValueError("OrbitOWL ha restituito un job inatteso.")
            status = job.get("status")
            if not isinstance(status, str) or status not in ACTIVE | TERMINAL | {"cancelled"}:
                raise ValueError("Stato OrbitOWL sconosciuto.")
            if status != last_status:
                print(f"OrbitOWL {job_id}: {status}", flush=True)
                last_status = status
            if status == "cancelled":
                print("Job annullato. Monitor terminato.", flush=True)
                return
            if status in TERMINAL:
                break
        time.sleep(POLL_SECONDS)

    print(f"Invio la richiesta di freeze al server Trooper.AI {server_id}.", flush=True)
    try:
        response = trooper_request("/api/order-status", trooper_key, {
            "serverId": server_id, "desired_status": "frozen", "migration_allowed": False,
        })
    except ValueError as exc:
        raise ValueError(f"Freeze non confermato. {exc} Verifica la dashboard Trooper.AI.") from exc
    if not isinstance(response, dict) or response.get("success") is not True:
        raise ValueError("Trooper.AI non ha confermato il freeze. Verifica la dashboard.")
    print("Richiesta di freeze accettata da Trooper.AI.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trooper-key-file", type=Path, default=Path(".orbitowl/trooper-api-key"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-servers", help="Mostra i server Trooper.AI e i relativi ID")
    watch = commands.add_parser("watch", help="Attendi la fine di un singolo job OrbitOWL")
    watch.add_argument("job_id")
    watch.add_argument("server_id", type=int)
    watch.add_argument("--url", default="http://127.0.0.1:8110")
    watch.add_argument("--token-file", type=Path, default=Path(".orbitowl/api-token"))
    args = parser.parse_args()

    try:
        key = read_key(args.trooper_key_file)
        if args.command == "list-servers":
            servers = trooper_request("/api/orders", key)
            if isinstance(servers, dict) and servers.get("success") is True:
                servers = servers.get("servers")
            if not isinstance(servers, list):
                raise ValueError("Risposta del catalogo server inattesa.")
            for server in servers:
                print(server["serverId"], server.get("title") or "-", server.get("machine_name") or "-", sep="\t")
            return
        if args.server_id <= 0:
            raise ValueError("Il server ID deve essere un intero positivo.")
        watch_job(args.url, read_key(args.token_file), identifier(args.job_id), args.server_id, key)
    except HTTPError as exc:
        parser.exit(1, f"Richiesta respinta (HTTP {exc.code}).\n")
    except (OSError, ServiceError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
