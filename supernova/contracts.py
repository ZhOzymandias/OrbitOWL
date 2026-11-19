"""Bounded original-byte transport; no extracted text is accepted in the manifest."""
import hashlib
import io
import json
import re
import uuid
import zipfile
from pathlib import PurePosixPath

from . import PROTOCOL

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 64


class ServiceError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def identifier(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ServiceError('invalid_identifier') from exc
    return value


def validate_bundle(data):
    if len(data) > MAX_BYTES:
        raise ServiceError('upload_too_large', 413)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [e.filename for e in entries]
            if (len(entries) > MAX_FILES+1 or len(names) != len(set(names))
                    or 'manifest.json' not in names or sum(e.file_size for e in entries) > MAX_BYTES
                    or any(e.flag_bits & 1 for e in entries)
                    or archive.getinfo('manifest.json').file_size > 131072):
                raise ServiceError('invalid_bundle')
            manifest = json.loads(archive.read('manifest.json'))
            if set(manifest) != {'protocol', 'build_id', 'snapshot_id', 'documents'}:
                raise ServiceError('invalid_manifest')
            if manifest['protocol'] != PROTOCOL:
                raise ServiceError('unsupported_protocol')
            identifier(manifest['build_id'])
            identifier(manifest['snapshot_id'])
            docs = manifest['documents']
            if not isinstance(docs, list) or not 1 <= len(docs) <= MAX_FILES:
                raise ServiceError('invalid_document_count')
            expected = {'manifest.json'}
            seen = set()
            for doc in docs:
                if set(doc) != {'id', 'filename', 'path', 'sha256'}:
                    raise ServiceError('invalid_document_manifest')
                identifier(doc['id'])
                filename = doc['filename']
                if (not isinstance(filename, str) or not 1 <= len(filename) <= 255
                        or PurePosixPath(filename).name != filename or '\\' in filename
                        or any(ord(c) < 32 for c in filename)):
                    raise ServiceError('invalid_filename')
                suffix = PurePosixPath(filename).suffix.lower()
                if suffix not in {'.pdf', '.txt', '.md'}:
                    raise ServiceError('unsupported_document_type')
                path = f"files/{doc['id']}{suffix}"
                if doc['path'] != path or doc['id'] in seen:
                    raise ServiceError('invalid_document_path')
                if not re.fullmatch('[0-9a-f]{64}', doc['sha256']):
                    raise ServiceError('invalid_document_hash')
                if path not in names or sha256(archive.read(path)) != doc['sha256']:
                    raise ServiceError('document_hash_mismatch')
                seen.add(doc['id'])
                expected.add(path)
            if set(names) != expected:
                raise ServiceError('unexpected_bundle_members')
            return manifest
    except ServiceError:
        raise
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ServiceError('invalid_bundle') from exc
