"""Use OrbitOWL from a terminal without installing client dependencies."""

import argparse
import io
import json
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from supernova import PROTOCOL
from supernova.contracts import (
    MAX_BYTES,
    MAX_FILES,
    ServiceError,
    identifier,
    sha256,
    validate_bundle,
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def make_bundle(files):
    if not 1 <= len(files) <= MAX_FILES or sum(p.stat().st_size for p in files) > MAX_BYTES:
        raise ValueError("Provide 1 to 64 documents; total size must be at most 64 MiB.")
    key = str(uuid.uuid4())
    manifest = {"protocol": PROTOCOL, "build_id": key, "snapshot_id": str(uuid.uuid4()), "documents": []}
    output = io.BytesIO()
    occurrences = Counter()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            data = path.read_bytes()
            checksum = sha256(data)
            identity = (path.name, checksum)
            ordinal = occurrences[identity]
            occurrences[identity] += 1
            document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["OrbitOWL", *identity, ordinal])))
            name = f"files/{document_id}{path.suffix.lower()}"
            bundle.writestr(name, data)
            manifest["documents"].append(
                {"id": document_id, "filename": path.name, "path": name, "sha256": checksum}
            )
        bundle.writestr("manifest.json", json.dumps(manifest))
    data = output.getvalue()
    validate_bundle(data)
    return key, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8110")
    parser.add_argument("--token-file", type=Path, default=Path(".orbitowl/api-token"))
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("ready")
    commands.add_parser("submit").add_argument("files", type=Path, nargs="+")
    commands.add_parser("status").add_argument("job")
    download = commands.add_parser("download")
    download.add_argument("job")
    download.add_argument("output", type=Path)
    args = parser.parse_args()
    data = None
    try:
        headers = {"Authorization": "Bearer " + args.token_file.read_text().strip()}
        path = "/health/ready"
        if args.action == "submit":
            key, data = make_bundle(args.files)
            path = "/v1/ontology-jobs"
            headers.update({"Content-Type": "application/zip", "Idempotency-Key": key})
            print(f"Job ID: {key}", flush=True)
        elif args.action in {"download", "status"}:
            path = "/v1/ontology-jobs/" + identifier(args.job)
            if args.action == "download":
                path += "/artifact"
        with build_opener(NoRedirect()).open(
            Request(args.url.rstrip("/") + path, data=data, headers=headers), timeout=60
        ) as response:
            result = response.read()
            if args.action == "download":
                if sha256(result) != response.headers.get("X-Artifact-SHA256"):
                    raise ValueError("Artifact checksum mismatch.")
                with args.output.open("xb") as out:
                    out.write(result)
                print(args.output)
            else:
                print(json.dumps(json.loads(result), indent=2))
    except HTTPError as exc:
        parser.exit(1, f"HTTP {exc.code}: {exc.read(1024).decode(errors='replace')}\n")
    except (URLError, OSError, ServiceError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
