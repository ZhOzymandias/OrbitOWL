"""One GPU worker, durable queue and raw output. Never repeats interrupted inference."""
import fcntl
import hashlib
import os
import time
from pathlib import Path

from . import SERVICE_REVISION
from .artifacts import normalize
from .contracts import ServiceError, canonical, sha256
from .documents import extract
from .runtime import NativeRuntime, messages
from .store import Store


def run_once(store, runtime):
    job = store.claim()
    if not job:
        return False
    job_id = job['id']
    directory = store.directory(job_id)
    report = {'engine': 'supernova-owl-v1', 'service_revision': SERVICE_REVISION, 'semantic_review': 'not_performed',
              'logical_check': 'not_run', 'input_mode': 'independent_original_files',
              'visual_content_processed': False}
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

        progress({})
        xml, usage = runtime.generate(messages(sources, iri), directory, progress, iri=iri)
        progress({})
        store.update(job_id, phase='normalizing')
        xml, checks = normalize(xml, expected_iri=iri, strict=True)
        (directory/'ontology.tmp').write_text(xml, encoding='utf-8')
        (directory/'ontology.tmp').replace(directory/'ontology.owl')
        report.update(checks, generation=usage, artifact_ready=True)
        store.update(job_id, status='completed', phase='completed', report=report,
                     artifact_sha256=sha256(xml.encode()))
    except ServiceError as exc:
        store.update(job_id, status='cancelled' if exc.code == 'job_cancelled' else 'failed',
                     error=exc.code, report=report)
    except Exception:
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
        path = Path(os.environ['SUPERNOVA_MODEL_PATH'])
        expected = os.environ['SUPERNOVA_MODEL_SHA256']
        hasher = hashlib.sha256()
        with path.open('rb') as model:
            for chunk in iter(lambda: model.read(8*1024*1024), b''):
                hasher.update(chunk)
        if hasher.hexdigest() != expected:
            raise SystemExit('Model checksum mismatch; no generation started')
        runtime = NativeRuntime(os.getenv('SUPERNOVA_RUNNER_URL', 'http://runner:8080'),
                                context=int(os.getenv('SUPERNOVA_CONTEXT', '65536')),
                                model_digest=expected)
        while True:
            (store.root/'worker-heartbeat').touch()
            if not run_once(store, runtime):
                time.sleep(2)


if __name__ == '__main__':
    main()
