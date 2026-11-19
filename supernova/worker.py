"""Single GPU worker with durable section checkpoints."""
import fcntl
import os
import time

from . import SERVICE_REVISION
from .artifacts import normalize
from .contracts import ServiceError, canonical, sha256
from .documents import extract
from .pipeline import build_ontology
from .runtime import REVISION, NativeRuntime
from .store import Store


def run_once(store, runtime):
    job = store.claim()
    if not job:
        return False
    job_id = job['id']
    directory = store.directory(job_id)
    report = {'engine': 'supernova-owl-v1', 'service_revision': SERVICE_REVISION, 'semantic_review': 'not_performed',
              'logical_check': 'not_run', 'input_mode': 'independent_original_files',
              'visual_content_processed': False, 'pipeline_revision': REVISION}
    try:
        manifest, sources = extract((directory/'input.zip').read_bytes())
        iri = 'urn:sinapsia:ontology:'+job_id
        report.update(build_id=job_id, snapshot_id=manifest['snapshot_id'],
                      documents=[{k: v for k, v in d.items() if k != 'pages'} for d in sources])
        (directory/'sources.json').write_bytes(canonical(sources))
        store.update(job_id, phase='generating', report=report)

        def progress(update):
            if store.get(job_id)['cancel_requested']:
                raise ServiceError('job_cancelled')
            (store.root/'worker-heartbeat').touch()
            report.update(update)
            store.update(job_id, report=report)

        progress({'stage': 'extracting_sections'})
        xml, usage = build_ontology(runtime, sources, directory, progress, iri=iri)
        progress({})
        store.update(job_id, phase='normalizing')
        xml, checks = normalize(xml, expected_iri=iri, strict=True)
        progress({})
        (directory/'ontology.tmp').write_text(xml, encoding='utf-8')
        (directory/'ontology.tmp').replace(directory/'ontology.owl')
        report.update(checks, generation=usage, artifact_ready=True, output_bytes=len(xml.encode()),
                      stage='completed')
        store.update(job_id, status='completed', phase='completed', report=report,
                     artifact_sha256=sha256(xml.encode()))
    except ServiceError as exc:
        store.update(job_id, status='cancelled' if exc.code == 'job_cancelled' else 'failed',
                     error=exc.code, report=report)
    except Exception:  # noqa: BLE001
        # Do not put corpus text or model responses into service/API logs.
        store.update(job_id, status='failed', error='generation_technical_error', report=report)
    return True


def main():
    store = Store(os.getenv('SUPERNOVA_DATA', '/data'))
    with (store.root/'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (store.root/'worker-heartbeat').unlink(missing_ok=True)
        (store.root/'worker-revision').write_text(SERVICE_REVISION, encoding='ascii')
        store.recover_interrupted()
        runtime = NativeRuntime(os.getenv('SUPERNOVA_RUNNER_URL', 'http://127.0.0.1:18081'),
                                context=int(os.getenv('SUPERNOVA_CONTEXT', '65536')),
                                model=os.getenv('ORBITOWL_MODEL', 'qwen3.8:27b'))
        while True:
            (store.root/'worker-heartbeat').touch()
            if not run_once(store, runtime):
                time.sleep(2)


if __name__ == '__main__':
    main()
