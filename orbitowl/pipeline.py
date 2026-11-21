"""Split before inference, persist validated sections, quarantine unrecoverable leaves, and resume."""

import re
from pathlib import Path

import httpx

from .compiler import compile_owl
from .contracts import ServiceError, canonical, sha256
from .runtime import REVISION
from .section_schema import decode_section, validate_section

SECTION_CHARS = 2400
OVERLAP_CHARS = 240
MIN_CHILD_CHARS = 24
MAX_SECTION_ATTEMPTS = 32
SPLIT_ERRORS = {
    "section_context_capacity_exceeded",
    "section_output_limit",
    "section_capacity_reached",
    "section_facts_unresolved",
}
RETRY_ERRORS = {"section_transport_error", "section_model_runtime_error", "section_stream_incomplete"}
QUARANTINE_ERRORS = {
    "section_attempt_budget_exhausted",
    "section_facts_unresolved",
    "section_output_limit",
    "section_capacity_reached",
    "section_schema_invalid",
    "section_json_invalid",
    "section_attribute_value_invalid",
    "section_attribute_fields_invalid",
    "section_attribute_entity_unknown",
    "section_relation_fields_invalid",
    "section_relation_types_invalid",
    "section_relation_entity_unknown",
    "section_quote_not_found",
    "section_passage_reference_invalid",
}


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical(value))
    temporary.replace(path)


def sections(sources):
    for document in sources:
        for page in document["pages"]:
            text = page["text"]
            start = 0
            while start < len(text):
                end = min(start + SECTION_CHARS, len(text))
                if end < len(text):
                    tail = text.rfind(" ", end - OVERLAP_CHARS, end)
                    if tail >= 0:
                        end = tail + 1
                if text[start:end].strip():
                    yield {
                        "document_id": document["document_id"],
                        "filename": document["filename"],
                        "page": page["number"],
                        "start": start,
                        "end": end,
                        "text": text[start:end],
                    }
                if end == len(text):
                    break
                start = end - OVERLAP_CHARS


def legacy_split_offsets(text):
    middle = len(text) // 2
    boundaries = [
        m.end() for m in re.finditer(r"\s+", text) if middle // 2 < m.end() < middle * 3 // 2
    ]
    point = min(boundaries, key=lambda value: abs(value - middle), default=middle)
    overlap = min(OVERLAP_CHARS // 2, len(text) // 8)
    return point + overlap, point - overlap


def split_offsets(text):
    middle = len(text) // 2
    minimum = max(MIN_CHILD_CHARS, len(text) // 4)
    for boundary in (r"\n[ \t]*\n+", r"(?<=[.!?;:])\s+", r"\s+"):
        offsets = [
            m.end()
            for m in re.finditer(boundary, text)
            if minimum <= m.end() <= len(text) - minimum
            and text[: m.end()].strip()
            and text[m.end() :].strip()
        ]
        if offsets:
            point = min(offsets, key=lambda offset: abs(offset - middle))
            # Include a little context only when breaking within a sentence.
            overlap = min(len(text) // 10, 48) if boundary == r"\s+" else 0
            return point + overlap, point - overlap
    return None


def split(section, offsets):
    text = section["text"]
    left_end, right_start = offsets
    return (
        {**section, "end": section["start"] + left_end, "text": text[:left_end]},
        {**section, "start": section["start"] + right_start, "text": text[right_start:]},
    )


def saved_split(marker, section):
    saved = decode_section(marker.read_text(encoding="utf-8"))
    return split(section, saved.get("offsets") or legacy_split_offsets(section["text"]))


def checkpoint_folders(root, signatures, section):
    return [root / sha256(canonical([signature, section])) for signature in signatures]


def load_section_checkpoint(folders, section):
    for index, folder in enumerate(folders):
        marker = folder / "split.json"
        if marker.is_file():
            return None, marker, None
        checkpoint = folder / "validated.json"
        if checkpoint.is_file():
            try:
                return (
                    validate_section(decode_section(checkpoint.read_text(encoding="utf-8")), section["text"]),
                    None,
                    None,
                )
            except ServiceError:
                continue
        # Reuse a quarantine marker only for the current signature (index 0).
        if index == 0:
            quarantined = folder / "quarantined.json"
            if quarantined.is_file():
                return None, None, decode_section(quarantined.read_text(encoding="utf-8"))
    return None, None, None


def count_reusable_sections(signature, sources, directory):
    root = Path(directory) / "sections"
    signatures = (signature,) if isinstance(signature, str) else signature

    def count(section):
        data, marker, _ = load_section_checkpoint(
            checkpoint_folders(root, signatures, section), section
        )
        if marker is not None:
            return sum(count(child) for child in saved_split(marker, section))
        return int(data is not None)

    return sum(count(section) for section in sections(sources))


def extract_sections(runtime, sources, directory, progress, *, previous_signatures=()):
    root = Path(directory) / "sections"
    root.mkdir(exist_ok=True, mode=0o700)
    planned = list(sections(sources))
    initial_planned = len(planned)
    max_skips = max(5, int(initial_planned * 0.20))
    stats = {
        "sections_total": initial_planned,
        "sections_completed": 0,
        "sections_reused": 0,
        "sections_split": 0,
        "sections_skipped": 0,
        "section_retries": 0,
    }
    results = []
    skipped = []
    last_error = None

    def generate(section, folder, budget):
        for attempt in range(2):
            used = decode_section(budget.read_text(encoding="utf-8")) if budget.exists() else 0
            if used >= MAX_SECTION_ATTEMPTS:
                raise ServiceError(
                    "section_attempt_budget_exhausted",
                    details={"attempts": used, "limit": MAX_SECTION_ATTEMPTS},
                )
            atomic_json(budget, used + 1)
            try:
                return runtime.generate_section(section, folder, progress)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise ServiceError("section_runner_rejected") from exc
                error = ServiceError("section_transport_error")
            except httpx.HTTPError:
                error = ServiceError("section_transport_error")
            except ServiceError as exc:
                if exc.code not in RETRY_ERRORS:
                    raise
                error = exc
            if attempt:
                raise error
            stats["section_retries"] += 1
            progress(stats)

    def read_one(section, budget):
        nonlocal last_error
        folders = checkpoint_folders(root, (runtime.signature, *previous_signatures), section)
        folder = folders[0]
        folder.mkdir(exist_ok=True, mode=0o700)
        location = {k: section[k] for k in ("document_id", "page", "start", "end")}
        progress(
            {**stats, "section": location, "section_output_bytes": 0, "section_input_tokens": None}
        )
        data, marker, quarantined = load_section_checkpoint(folders, section)
        if marker is not None:
            stats["sections_split"] += 1
            stats["sections_total"] += 1
            for part in saved_split(marker, section):
                read_one(part, budget)
            return
        if quarantined is not None:
            stats["sections_skipped"] += 1
            skipped.append(quarantined)
            progress({**stats, "last_skipped_section": quarantined})
            return
        try:
            if data is None:
                data = validate_section(generate(section, folder, budget), section["text"])
            else:
                stats["sections_reused"] += 1
            atomic_json(folder / "validated.json", data)
        except ServiceError as exc:
            offsets = split_offsets(section["text"]) if exc.code in SPLIT_ERRORS else None
            if offsets is None:
                failed = {**location, "error": exc.code, **exc.details}
                if exc.code in SPLIT_ERRORS:
                    failed["stop_reason"] = "no_useful_text_boundary"
                if exc.code in QUARANTINE_ERRORS and initial_planned > 1:
                    atomic_json(folder / "quarantined.json", failed)
                    stats["sections_skipped"] += 1
                    skipped.append(failed)
                    last_error = exc
                    progress({**stats, "last_skipped_section": failed})
                    if stats["sections_skipped"] > max_skips:
                        progress({"failed_section": failed})
                        raise ServiceError(
                            "too_many_skipped_sections",
                            details={
                                "sections_skipped": stats["sections_skipped"],
                                "limit": max_skips,
                                "last_error": exc.code,
                            },
                        ) from exc
                    return
                if exc.code != "job_cancelled":
                    progress({"failed_section": failed})
                raise
            atomic_json(folder / "split.json", {"trigger": exc.code, "offsets": offsets, **exc.details})
            stats["sections_split"] += 1
            stats["sections_total"] += 1
            progress({**stats, "last_split": {**location, "error": exc.code, **exc.details}})
            for part in split(section, offsets):
                read_one(part, budget)
            return
        results.append((section, data))
        stats["sections_completed"] += 1
        progress(stats)

    for section in planned:
        key = sha256(canonical([runtime.signature, section]))
        read_one(section, root / f"{key}.attempts.json")

    if skipped:
        atomic_json(Path(directory) / "skipped-sections.json", skipped)
        if stats["sections_completed"] == 0 and last_error is not None:
            raise last_error
    return results, stats


def build_ontology(runtime, sources, directory, progress, *, iri, previous_signature=None):
    previous = (previous_signature,) if previous_signature else ()
    results, stats = extract_sections(
        runtime, sources, directory, progress, previous_signatures=previous
    )
    progress({"stage": "compiling", "section": None})
    xml, counts = compile_owl(iri, results)
    return xml, {"prompt_revision": REVISION, **stats, **counts}
