"""Split before inference, persist validated sections, and resume by source span."""

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
    'section_context_capacity_exceeded', 'section_output_limit', 'section_capacity_reached',
}
CORRECTION_ERRORS = {
    'section_quote_not_found', 'section_relation_types_invalid',
    'section_relation_entity_unknown', 'section_attribute_entity_unknown',
}
RETRY_ERRORS = {'section_transport_error', 'section_model_runtime_error', 'section_stream_incomplete'}


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(canonical(value))
    temporary.replace(path)


def sections(sources):
    for document in sources:
        for page in document['pages']:
            text = page['text']
            start = 0
            while start < len(text):
                end = min(start + SECTION_CHARS, len(text))
                if end < len(text):
                    tail = text.rfind(' ', end - OVERLAP_CHARS, end)
                    if tail >= 0:
                        end = tail + 1
                if text[start:end].strip():
                    yield {'document_id': document['document_id'], 'filename': document['filename'],
                           'page': page['number'], 'start': start, 'end': end, 'text': text[start:end]}
                if end == len(text):
                    break
                start = end - OVERLAP_CHARS


def legacy_split_offsets(text):
    middle = len(text) // 2
    boundaries = [m.end() for m in re.finditer(r'\s+', text) if middle // 2 < m.end() < middle * 3 // 2]
    point = min(boundaries, key=lambda value: abs(value-middle), default=middle)
    overlap = min(OVERLAP_CHARS // 2, len(text) // 8)
    return point+overlap, point-overlap


def split_offsets(text):
    middle = len(text) // 2
    minimum = max(MIN_CHILD_CHARS, len(text) // 4)
    for boundary in (r"\n[ \t]*\n+", r"(?<=[.!?;:])\s+", r"\s+"):
        offsets = [m.end() for m in re.finditer(boundary, text)
                   if minimum <= m.end() <= len(text)-minimum
                   and text[:m.end()].strip() and text[m.end():].strip()]
        if offsets:
            point = min(offsets, key=lambda offset: abs(offset-middle))
            # Include a little context only when breaking within a sentence.
            overlap = min(len(text) // 10, 48) if boundary == r"\s+" else 0
            return point+overlap, point-overlap
    return None


def split(section, offsets):
    text = section['text']
    left_end, right_start = offsets
    return (
        {**section, 'end': section['start']+left_end, 'text': text[:left_end]},
        {**section, 'start': section['start']+right_start, 'text': text[right_start:]},
    )


def saved_split(marker, section):
    saved = decode_section(marker.read_text(encoding='utf-8'))
    return split(section, saved.get('offsets') or legacy_split_offsets(section['text']))


def count_reusable_sections(signature, sources, directory):
    root = Path(directory) / 'sections'

    def count(section):
        folder = root / sha256(canonical([signature, section]))
        marker = folder / 'split.json'
        if marker.is_file():
            return sum(count(child) for child in saved_split(marker, section))
        checkpoint = folder / 'validated.json'
        if not checkpoint.is_file():
            return 0
        try:
            validate_section(decode_section(checkpoint.read_text(encoding='utf-8')), section['text'])
        except ServiceError:
            return 0
        return 1

    return sum(count(section) for section in sections(sources))


def build_ontology(runtime, sources, directory, progress, *, iri, previous_signature=None):
    root = Path(directory) / 'sections'
    root.mkdir(exist_ok=True, mode=0o700)
    planned = list(sections(sources))
    stats = {'sections_total': len(planned), 'sections_completed': 0,
             'sections_reused': 0, 'sections_split': 0, 'section_retries': 0,
             'section_corrections': 0}
    results = []

    def generate(section, folder, budget, *, correction=None):
        attempts = 1 if correction is not None else 2
        for attempt in range(attempts):
            used = decode_section(budget.read_text(encoding='utf-8')) if budget.exists() else 0
            if used >= MAX_SECTION_ATTEMPTS:
                raise ServiceError('section_attempt_budget_exhausted',
                                   details={'attempts': used, 'limit': MAX_SECTION_ATTEMPTS})
            atomic_json(budget, used + 1)
            try:
                if correction is not None:
                    return runtime.generate_section(section, folder, progress, correction=correction)
                return runtime.generate_section(section, folder, progress)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise ServiceError('section_runner_rejected') from exc
                error = ServiceError('section_transport_error')
            except httpx.HTTPError:
                error = ServiceError('section_transport_error')
            except ServiceError as exc:
                if exc.code not in RETRY_ERRORS:
                    raise
                error = exc
            if attempt + 1 == attempts:
                raise error
            stats['section_retries'] += 1
            progress(stats)

    def extract_section(section, folder, budget):
        marker = folder / 'correction.json'
        if marker.exists():
            failure = decode_section(marker.read_text(encoding='utf-8'))
            raise ServiceError('section_correction_failed', details=failure)
        generated = generate(section, folder, budget)
        try:
            return validate_section(generated, section['text'])
        except ServiceError as exc:
            if exc.code not in CORRECTION_ERRORS:
                raise
            issue = {'code': exc.code, **exc.details}
        # A restart must not buy another correction for the same span.
        failure = {'original_error': issue, 'correction_error': {'code': 'section_correction_interrupted'}}
        atomic_json(marker, failure)
        correction_dir = folder / 'correction'
        correction_dir.mkdir(exist_ok=True, mode=0o700)
        stats['section_corrections'] += 1
        progress(stats)
        try:
            corrected = generate(section, correction_dir, budget,
                                 correction={'issue': issue, 'previous': generated})
            data = validate_section(corrected, section['text'])
            if not (data['relations'] or data['attributes']):
                raise ServiceError('section_correction_removed_all_facts')
            return data
        except ServiceError as exc:
            if exc.code == 'job_cancelled':
                raise
            failure['correction_error'] = {'code': exc.code, **exc.details}
            atomic_json(marker, failure)
            raise ServiceError('section_correction_failed', details=failure) from exc

    def read_one(section, budget):
        key = sha256(canonical([runtime.signature, section]))
        folder = root / key
        folder.mkdir(exist_ok=True, mode=0o700)
        completed = folder / 'validated.json'
        split_flag = folder / 'split.json'
        if previous_signature:
            previous_folder = root / sha256(canonical([previous_signature, section]))
            previous_split = previous_folder / 'split.json'
            if not completed.exists() and not split_flag.exists() and previous_split.exists():
                split_flag = previous_split
        location = {k: section[k] for k in ('document_id', 'page', 'start', 'end')}
        progress({**stats, 'section': location, 'section_output_bytes': 0, 'section_input_tokens': None})
        if split_flag.exists():
            stats['sections_split'] += 1
            stats['sections_total'] += 1
            for part in saved_split(split_flag, section):
                read_one(part, budget)
            return
        try:
            if completed.exists():
                data = validate_section(decode_section(completed.read_text(encoding='utf-8')), section['text'])
                stats['sections_reused'] += 1
            else:
                data = None
                if previous_signature:
                    previous_completed = previous_folder / 'validated.json'
                    if previous_completed.exists():
                        try:
                            data = validate_section(
                                decode_section(previous_completed.read_text(encoding='utf-8')),
                                section['text'],
                            )
                        except ServiceError:
                            pass
                        else:
                            stats['sections_reused'] += 1
                if data is None:
                    data = extract_section(section, folder, budget)
                atomic_json(completed, data)
        except ServiceError as exc:
            offsets = split_offsets(section['text']) if exc.code in SPLIT_ERRORS else None
            if offsets is None:
                if exc.code != 'job_cancelled':
                    failed = {**location, 'error': exc.code, **exc.details}
                    if exc.code in SPLIT_ERRORS:
                        failed['stop_reason'] = 'no_useful_text_boundary'
                    progress({'failed_section': failed})
                raise
            atomic_json(split_flag, {'trigger': exc.code, 'offsets': offsets, **exc.details})
            stats['sections_split'] += 1
            stats['sections_total'] += 1
            progress({**stats, 'last_split': {**location, 'error': exc.code, **exc.details}})
            for part in split(section, offsets):
                read_one(part, budget)
            return
        results.append((section, data))
        stats['sections_completed'] += 1
        progress(stats)

    for section in planned:
        key = sha256(canonical([runtime.signature, section]))
        read_one(section, root / f'{key}.attempts.json')
    progress({'stage': 'compiling', 'section': None})
    xml, counts = compile_owl(iri, results)
    return xml, {'prompt_revision': REVISION, **stats, **counts}
