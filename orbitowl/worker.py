"""Single GPU worker with durable section checkpoints."""

import argparse
import json
import os
import time

from . import ENGINE, SERVICE_REVISION
from .artifacts import normalize
from .contracts import ServiceError, canonical, sha256
from .documents import extract
from .incremental import build_incremental
from .pipeline import count_reusable_sections
from .runtime import REVISION, NativeRuntime
from .store import Store


def checkpoint_revisions(report, runtime):
    revisions = (
        report.get("pipeline_revision"),
        *report.get("checkpoint_revisions", ()),
        report.get("checkpoint_revision"),
    )
    return tuple(
        dict.fromkeys(
            revision
            for revision in revisions
            if revision in getattr(runtime, "previous_signatures", {})
        )
    )


def run_once(store, runtime):
    job = store.claim()
    if not job:
        return False
    job_id = job["id"]
    directory = store.directory(job_id)
    report = {
        "engine": ENGINE,
        "service_revision": SERVICE_REVISION,
        "semantic_review": "not_performed",
        "logical_check": "not_run",
        "input_mode": "independent_original_files",
        "visual_content_processed": False,
        "pipeline_revision": REVISION,
    }
    revisions = checkpoint_revisions(job["report"], runtime)
    if revisions:
        report["checkpoint_revisions"] = revisions
    try:
        manifest, sources = extract((directory / "input.zip").read_bytes())
        iri = "urn:sinapsia:ontology:" + job_id
        report.update(
            build_id=job_id,
            snapshot_id=manifest["snapshot_id"],
            documents=[{k: v for k, v in d.items() if k != "pages"} for d in sources],
        )
        (directory / "sources.json").write_bytes(canonical(sources))
        store.update(job_id, phase="generating", report=report)

        def progress(update):
            if store.get(job_id)["cancel_requested"]:
                raise ServiceError("job_cancelled")
            (store.root / "worker-heartbeat").touch()
            report.update(update)
            store.update(job_id, report=report)

        progress({"stage": "extracting_sections"})
        xml, usage = build_incremental(
            runtime,
            sources,
            directory,
            progress,
            iri=iri,
            previous_signatures=tuple(
                runtime.previous_signatures[revision] for revision in revisions
            ),
        )
        progress({})
        store.update(job_id, phase="normalizing")
        xml, checks = normalize(xml, expected_iri=iri, strict=True)
        progress({})
        xml_bytes = xml.encode("utf-8")
        (directory / "ontology.tmp").write_bytes(xml_bytes)
        (directory / "ontology.tmp").replace(directory / "ontology.owl")
        report.update(
            checks,
            generation=usage,
            artifact_ready=True,
            output_bytes=len(xml_bytes),
            stage="completed",
        )
        store.update(
            job_id,
            status="completed",
            phase="completed",
            report=report,
            artifact_sha256=sha256(xml_bytes),
        )
    except ServiceError as exc:
        store.update(
            job_id,
            status="cancelled" if exc.code == "job_cancelled" else "failed",
            error=exc.code,
            report=report,
        )
    except Exception:  # noqa: BLE001
        # Do not put corpus text or model responses into service/API logs.
        store.update(job_id, status="failed", error="generation_technical_error", report=report)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument(
        "--check-resume",
        metavar="JOB_ID",
        help="Check which older sections can be reused without inference",
    )
    resume.add_argument(
        "--resume",
        metavar="JOB_ID",
        help="Requeue a compatible job stopped by section budget or validation",
    )
    args = parser.parse_args()
    data_dir = os.getenv("ORBITOWL_DATA") or os.getenv("SUPERNOVA_DATA", "/data")
    runner_url = (
        os.getenv("ORBITOWL_RUNNER_URL")
        or os.getenv("SUPERNOVA_RUNNER_URL", "http://127.0.0.1:18081")
    )
    context = int(os.getenv("ORBITOWL_CONTEXT") or os.getenv("SUPERNOVA_CONTEXT", "65536"))
    store = Store(data_dir)
    runtime = NativeRuntime(
        runner_url,
        context=context,
        model=os.getenv("ORBITOWL_MODEL", "qwen3.8:27b"),
    )
    job_id = args.resume if args.resume is not None else args.check_resume
    if job_id is not None:
        try:
            job = store.resume_section_budget_failure(job_id, preview=True)
            directory = store.directory(job_id)
            _, sources = extract((directory / "input.zip").read_bytes())
            signatures = tuple(
                runtime.previous_signatures[revision]
                for revision in checkpoint_revisions(job["report"], runtime)
            )
            reusable = count_reusable_sections(signatures, sources, directory)
            expected = job["report"].get("sections_completed", 0)
            if args.resume is not None:
                if reusable < expected:
                    raise ServiceError(
                        "job_resume_checkpoint_mismatch",
                        details={"sections_expected": expected, "sections_reusable": reusable},
                    )
                job = store.resume_section_budget_failure(job_id)
        except ServiceError as exc:
            parser.exit(1, json.dumps({"error": exc.code, **exc.details}) + "\n")
        print(
            json.dumps(
                {
                    "id": job["id"],
                    "status": job["status"],
                    "sections_expected": expected,
                    "sections_reusable": reusable,
                }
            )
        )
        return
    import fcntl

    with (store.root / "worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (store.root / "worker-heartbeat").unlink(missing_ok=True)
        (store.root / "worker-revision").write_text(SERVICE_REVISION, encoding="ascii")
        store.recover_interrupted()
        while True:
            (store.root / "worker-heartbeat").touch()
            if not run_once(store, runtime):
                time.sleep(2)


if __name__ == "__main__":
    main()
