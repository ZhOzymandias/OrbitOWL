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
    'section_context_capacity_exceeded', 'section_output_limit', 'section_json_invalid',
    'section_schema_invalid', 'section_quote_not_found', 'section_attribute_value_invalid',
    'section_capacity_reached', 'section_relation_types_invalid',
    'section_relation_entity_unknown', 'section_relation_fields_invalid',
    'section_attribute_entity_unknown', 'section_attribute_fields_invalid',
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


def build_ontology(runtime, sources, directory, progress, *, iri):
    root = Path(directory) / 'sections'
    root.mkdir(exist_ok=True, mode=0o700)
    planned = list(sections(sources))
    stats = {'sections_total': len(planned), 'sections_completed': 0,
             'sections_reused': 0, 'sections_split': 0, 'section_retries': 0}
    results = []

    def generate(section, folder, budget):
        for attempt in range(2):
            used = decode_section(budget.read_text(encoding='utf-8')) if budget.exists() else 0
            if used >= MAX_SECTION_ATTEMPTS:
                raise ServiceError('section_attempt_budget_exhausted',
                                   details={'attempts': used, 'limit': MAX_SECTION_ATTEMPTS})
            atomic_json(budget, used + 1)
            try:
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
            if attempt:
                raise error
            stats['section_retries'] += 1
            progress(stats)

    def read_one(section, budget):
        key = sha256(canonical([runtime.signature, section]))
        folder = root / key
        folder.mkdir(exist_ok=True, mode=0o700)
        completed = folder / 'validated.json'
        split_flag = folder / 'split.json'
        location = {k: section[k] for k in ('document_id', 'page', 'start', 'end')}
        progress({**stats, 'section': location, 'section_output_bytes': 0, 'section_input_tokens': None})
        if split_flag.exists():
            stats['sections_split'] += 1
            stats['sections_total'] += 1
            saved = decode_section(split_flag.read_text(encoding='utf-8'))
            offsets = saved.get('offsets') or legacy_split_offsets(section['text'])
            for part in split(section, offsets):
                read_one(part, budget)
            return
        try:
            if completed.exists():
                data = validate_section(decode_section(completed.read_text(encoding='utf-8')), section['text'])
                stats['sections_reused'] += 1
            else:
                generated = generate(section, folder, budget)
                data = validate_section(generated, section['text'])
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
