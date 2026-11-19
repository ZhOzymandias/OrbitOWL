"""Extraction performed on Supernova from the original received file bytes."""
import io
import zipfile
from pathlib import PurePosixPath

from pypdf import PdfReader

from .contracts import ServiceError, validate_bundle


def extract(data):
    manifest = validate_bundle(data)
    sources = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for doc in manifest['documents']:
            content = archive.read(doc['path'])
            suffix = PurePosixPath(doc['filename']).suffix.lower()
            if suffix == '.pdf':
                try:
                    reader = PdfReader(io.BytesIO(content))
                    if reader.is_encrypted or not 1 <= len(reader.pages) <= 512:
                        raise ServiceError('pdf_encrypted_or_page_limit')
                    pages = [page.extract_text() or '' for page in reader.pages]
                except ServiceError:
                    raise
                except Exception as exc:
                    raise ServiceError('pdf_extraction_failed') from exc
                if any(not text.strip() for text in pages):
                    raise ServiceError('pdf_needs_ocr')
            else:
                try:
                    pages = [content.decode('utf-8-sig')]
                except UnicodeError as exc:
                    raise ServiceError('text_encoding_unsupported') from exc
            if not any(p.strip() for p in pages):
                raise ServiceError('empty_document')
            sources.append({'document_id': doc['id'], 'filename': doc['filename'],
                            'sha256': doc['sha256'],
                            'pages': [{'number': i, 'text': text} for i, text in enumerate(pages, 1)]})
    return manifest, sources
