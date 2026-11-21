"""Small authenticated ASGI API; expensive PDF/model work belongs to the worker."""
import asyncio
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

from . import SERVICE_REVISION
from .contracts import MAX_BYTES, ServiceError, identifier
from .store import Store


def read_token():
    path = os.getenv('ORBITOWL_TOKEN_FILE') or os.getenv('SUPERNOVA_TOKEN_FILE', '')
    return Path(path).read_text().strip() if path else ''


def _data_dir():
    return os.getenv('ORBITOWL_DATA') or os.getenv('SUPERNOVA_DATA', '/data')


def _runner_url():
    return os.getenv('ORBITOWL_RUNNER_URL') or os.getenv('SUPERNOVA_RUNNER_URL', 'http://127.0.0.1:18081')


class Application:
    def __init__(self, store=None, token=None, ready=None):
        self.store, self.token, self.ready = store, token, ready

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        if scope['type'] != 'http':
            return
        status, payload, content_type, extra = 200, {}, b'application/json', []
        try:
            path, method = scope['path'], scope['method']
            if path == '/health/live' and method == 'GET':
                payload = {'status': 'alive'}
            else:
                token = self.token if self.token is not None else read_token()
                if len(token) < 32:
                    raise ServiceError('service_not_configured', 503)
                headers = {k.lower(): v for k, v in scope.get('headers', [])}
                if not hmac.compare_digest(headers.get(b'authorization', b''), ('Bearer '+token).encode()):
                    raise ServiceError('unauthorized', 401)
                if path == '/health/ready' and method == 'GET':
                    if self.ready:
                        ready = self.ready()
                    else:
                        async with httpx.AsyncClient(timeout=5) as client:
                            response = await client.get(_runner_url()+'/health')
                            ready = response.status_code == 200
                    if self.ready is None:
                        heartbeat = Path(_data_dir())/'worker-heartbeat'
                        revision = heartbeat.with_name('worker-revision')
                        ready = (ready and heartbeat.is_file() and time.time()-heartbeat.stat().st_mtime < 60
                                 and revision.is_file() and revision.read_text() == SERVICE_REVISION)
                    if not ready:
                        raise ServiceError('model_not_ready', 503)
                    payload = {'status': 'ready', 'service_revision': SERVICE_REVISION}
                else:
                    store = self.store or Store(_data_dir())
                    parts = path.strip('/').split('/')
                    if parts == ['v1', 'ontology-jobs'] and method == 'POST':
                        if headers.get(b'content-type', b'').split(b';')[0] != b'application/zip':
                            raise ServiceError('zip_required', 415)
                        data = bytearray()
                        while True:
                            event = await receive()
                            if event['type'] == 'http.disconnect':
                                return
                            data.extend(event.get('body', b''))
                            if len(data) > MAX_BYTES:
                                raise ServiceError('upload_too_large', 413)
                            if not event.get('more_body'):
                                break
                        key = headers.get(b'idempotency-key', b'').decode('ascii')
                        payload = await asyncio.to_thread(store.submit, bytes(data), key)
                        status = 202
                    elif len(parts) in (3, 4) and parts[:2] == ['v1', 'ontology-jobs']:
                        job_id = identifier(parts[2])
                        if len(parts) == 4 and parts[3] == 'cancel' and method == 'POST':
                            payload = store.cancel(job_id)
                        elif len(parts) == 3 and method == 'GET':
                            payload = store.get(job_id)
                        elif len(parts) == 4 and parts[3] == 'artifact' and method == 'GET':
                            job = store.get(job_id)
                            if job['status'] != 'completed':
                                raise ServiceError('artifact_not_ready', 409)
                            payload = (store.directory(job_id)/'ontology.owl').read_bytes()
                            content_type = b'application/rdf+xml'
                            extra = [(b'x-artifact-sha256', job['artifact_sha256'].encode())]
                        else:
                            raise ServiceError('not_found', 404)
                    else:
                        raise ServiceError('not_found', 404)
        except ServiceError as exc:
            status, payload = exc.status, {'error': exc.code}
        except (OSError, sqlite3.Error, httpx.HTTPError):
            status, payload = 503, {'error': 'service_unavailable'}
        except (ValueError, UnicodeError):
            status, payload = 400, {'error': 'invalid_request'}
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', content_type), (b'cache-control', b'no-store'),
                                (b'content-length', str(len(body)).encode()), *extra]})
        await send({'type': 'http.response.body', 'body': body})


app = Application()
