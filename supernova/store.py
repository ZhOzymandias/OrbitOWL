"""Durable single-worker queue shared with the ASGI API, no in-memory jobs."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .contracts import ServiceError, canonical, identifier, sha256, validate_bundle
from .runtime import PREVIOUS_REVISIONS


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                phase TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '{}',
                artifact_sha256 TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root/'jobs.sqlite3', timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def directory(self, job_id):
        return self.root/identifier(job_id)

    def get(self, job_id):
        identifier(job_id)
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ServiceError('job_not_found', 404)
        result = dict(row)
        result['report'] = json.loads(result['report'])
        result.pop('fingerprint')
        return result

    def submit(self, data, key):
        manifest = validate_bundle(data)
        if key != manifest['build_id']:
            raise ServiceError('idempotency_key_mismatch', 409)
        fingerprint = sha256(canonical(manifest))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint FROM jobs WHERE id=?', (key,)).fetchone()
            if row:
                if row['fingerprint'] and row['fingerprint'] != fingerprint:
                    raise ServiceError('idempotency_conflict', 409)
            else:
                directory = self.directory(key)
                directory.mkdir(exist_ok=True, mode=0o700)
                temp = directory/'input.tmp'
                temp.write_bytes(data)
                temp.replace(directory/'input.zip')
                db.execute('INSERT INTO jobs(id,fingerprint,status,phase,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                           (key, fingerprint, 'queued', 'queued', time.time(), time.time()))
        return self.get(key)

    def update(self, job_id, **fields):
        if not set(fields) <= {'status', 'phase', 'error', 'report', 'artifact_sha256'}:
            raise ValueError('Invalid update')
        if 'report' in fields:
            fields['report'] = json.dumps(fields['report'], ensure_ascii=False)
        fields['updated_at'] = time.time()
        with self.connect() as db:
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',
                       (*fields.values(), identifier(job_id)))

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM jobs WHERE status='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running',phase='preparing',updated_at=? WHERE id=?", (time.time(), row['id']))
        return self.get(row['id']) if row else None

    def cancel(self, job_id):
        identifier(job_id)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,fingerprint,status,phase,error,cancel_requested,created_at,updated_at) VALUES(?, '', 'cancelled', 'cancelled', 'cancelled_before_submission', 1, ?, ?)", (job_id, time.time(), time.time()))
            db.execute("UPDATE jobs SET cancel_requested=1, status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END, updated_at=? WHERE id=?", (time.time(), job_id))
        return self.get(job_id)

    def resume_section_budget_failure(self, job_id, *, preview=False):
        directory = self.directory(job_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(
                'SELECT status,error,report,cancel_requested,artifact_sha256 FROM jobs WHERE id=?',
                (job_id,),
            ).fetchone()
            if row is None:
                raise ServiceError('job_not_found', 404)
            if (row['status'] != 'failed' or row['error'] not in {'section_attempt_budget_exhausted', 'section_correction_failed'}
                    or row['cancel_requested'] or row['artifact_sha256']
                    or json.loads(row['report']).get('pipeline_revision') not in PREVIOUS_REVISIONS):
                raise ServiceError('job_resume_not_applicable', 409)
            if not (directory/'input.zip').is_file() or not (directory/'sections').is_dir():
                raise ServiceError('job_resume_checkpoint_missing', 409)
            if not preview:
                db.execute("UPDATE jobs SET status='queued',phase='queued',error='',updated_at=? WHERE id=?",
                           (time.time(), job_id))
        return self.get(job_id)

    def recover_interrupted(self):
        # Called only under the exclusive worker lock. Legacy jobs have no section checkpoints.
        with self.connect() as db:
            now = time.time()
            db.execute("""UPDATE jobs SET status='cancelled',phase='cancelled',error='job_cancelled',
                        updated_at=? WHERE status='running' AND cancel_requested=1""", (now,))
            db.execute("""UPDATE jobs SET status='queued',phase='queued',error='',updated_at=?
                        WHERE status='running' AND json_extract(report,'$.pipeline_revision')
                        LIKE 'structured-sections-%'""", (now,))
            db.execute("""UPDATE jobs SET status='failed',error='worker_interrupted',updated_at=?
                        WHERE status='running'""", (now,))
