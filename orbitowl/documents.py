"""Extraction performed on OrbitOWL from the original received file bytes."""

import io
import zipfile
from pathlib import PurePosixPath

from pypdf import PdfReader

from .contracts import ServiceError, validate_bundle

MAX_PDF_PAGES = 2000


def extract(data):
    manifest = validate_bundle(data)
    sources = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for doc in manifest["documents"]:
            content = archive.read(doc["path"])
            suffix = PurePosixPath(doc["filename"]).suffix.lower()
            empty_pages = 0
            if suffix == ".pdf":
                try:
                    reader = PdfReader(io.BytesIO(content))
                    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
                        raise ServiceError("pdf_encrypted_or_page_limit")
                    raw_pages = [page.extract_text() or "" for page in reader.pages]
                except ServiceError:
                    raise
                except Exception as exc:
                    raise ServiceError("pdf_extraction_failed") from exc
                empty_pages = sum(1 for text in raw_pages if not text.strip())
                if empty_pages == len(raw_pages):
                    raise ServiceError("pdf_needs_ocr")
                pages = [
                    {"number": i, "text": text}
                    for i, text in enumerate(raw_pages, 1)
                    if text.strip()
                ]
            else:
                try:
                    text = content.decode("utf-8-sig")
                except UnicodeError as exc:
                    raise ServiceError("text_encoding_unsupported") from exc
                if not text.strip():
                    raise ServiceError("empty_document")
                pages = [{"number": 1, "text": text}]
            entry = {
                "document_id": doc["id"],
                "filename": doc["filename"],
                "sha256": doc["sha256"],
                "pages": pages,
            }
            if empty_pages:
                entry["empty_pages"] = empty_pages
            sources.append(entry)
    return manifest, sources
