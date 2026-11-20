"""Assemble an ontology from independently validated document updates."""

import re
from collections import Counter, defaultdict
from pathlib import Path

from .compiler import compile_owl
from .contracts import ServiceError, canonical, sha256
from .pipeline import atomic_json, extract_sections, sections
from .runtime import REVISION
from .section_schema import clean, decode_section, entity_key, identity, validate_section

ALIAS_WORDS = r'anche (?:dett[oaie]|chiamat[oaie]|denominat[oaie])|alias|also known as'
ALIAS_MARKER = re.compile(rf',? (?:{ALIAS_WORDS}) ')
ACRONYM = re.compile(r'\(([A-Z][A-Z0-9]{1,11})\)')


class Concepts:
    """Resolve source-attested aliases while protecting kind and scope."""

    def __init__(self):
        self.labels = {}
        self.by_first_word = defaultdict(list)
        self.aliases = {}
        self.evidence = []
        self.claims = []
        self.subclasses = set()

    def resolve(self, key):
        while key in self.aliases:
            key = self.aliases[key]
        return key

    def add_document(self, records):
        new_keys = set()
        for _, data in records:
            keys = {row['id']: entity_key(row) for row in data['entities']}
            for row in data['entities']:
                key = keys[row['id']]
                new_keys.add(key)
                if key not in self.labels:
                    self.labels[key] = (len(self.labels), clean(row['label']))
                    first_word = key[1].split()[0]
                    self.by_first_word[first_word].append(key)
            for row in data['relations']:
                if row['kind'] == 'subclass':
                    self.subclasses.add((keys[row['source']], keys[row['target']]))

        # Later documents can establish a hierarchy that invalidates an earlier merge.
        self.aliases.clear()
        self.evidence.clear()
        for claim in self.claims:
            self.merge(*claim)
        for section, _ in records:
            text = clean(section['text'])
            normalized = identity(text)
            if not ALIAS_MARKER.search(normalized) and not ACRONYM.search(text):
                continue
            words = set(normalized.split())
            candidates = {
                key for word in words for key in self.by_first_word.get(word.strip('(),.;:"«»'), ())
            }
            candidates = sorted(candidates, key=lambda key: (-len(key[1]), key))
            for newcomer in candidates:
                if newcomer not in new_keys:
                    continue
                matches = []
                for candidate in candidates:
                    if newcomer == candidate or (newcomer[0], newcomer[2]) != (candidate[0], candidate[2]):
                        continue
                    quote = alias_quote(text, self.labels[newcomer][1], self.labels[candidate][1])
                    if quote:
                        matches.append((candidate, quote))
                # A label with several different attested candidates needs explicit review.
                if len({self.resolve(candidate) for candidate, _ in matches}) != 1:
                    continue
                candidate, quote = matches[0]
                claim = (newcomer, candidate, section, quote)
                self.claims.append(claim)
                self.merge(*claim)

    def merge(self, left, right, section, quote):
        left, right = self.resolve(left), self.resolve(right)
        if left == right:
            return
        if any({self.resolve(child), self.resolve(parent)} == {left, right}
               for child, parent in self.subclasses):
            return
        first, second = sorted((left, right), key=lambda key: self.labels[key][0])
        self.aliases[second] = first
        self.evidence.append((second, section, quote))


def alias_quote(text, first, second):
    for left, right in ((first, second), (second, first)):
        a, b = re.escape(left), re.escape(right)
        pattern = (
            rf'(?<!\w){a}(?:,? )'
            rf'(?:{ALIAS_WORDS})'
            rf' ["«]?{b}(?!\w)'
        )
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group()
        initials = ''.join(word[0] for word in re.findall(r'\w+', left) if word).upper()
        if right.upper() == initials and ACRONYM.fullmatch(f'({initials})'):
            match = re.search(rf'(?<!\w)(?i:{a}) \({re.escape(initials)}\)', text)
            if match:
                return match.group()
    return None


def document_records(path, document):
    """Read a completed document cache and recheck it against the current source."""
    records = decode_section(path.read_text(encoding='utf-8'))
    pages = {page['number']: page['text'] for page in document['pages']}
    for section, data in records:
        text = pages[section['page']]
        if (section['document_id'] != document['document_id']
                or section['filename'] != document['filename']
                or not 0 <= section['start'] < section['end'] <= len(text)
                or section['text'] != text[section['start']:section['end']]):
            raise ServiceError('document_checkpoint_source_mismatch')
        validate_section(data, section['text'])
    return records


def build_incremental(runtime, sources, directory, progress, *, iri, previous_signatures=()):
    cache = Path(directory).parent / 'validated-documents'
    cache.mkdir(exist_ok=True, mode=0o700)
    updates = Path(directory) / 'document-updates'
    updates.mkdir(exist_ok=True, mode=0o700)
    graph = Concepts()
    records = []
    completed = Counter()
    planned = [sum(1 for _ in sections([document])) for document in sources]
    reused_documents = 0
    for index, document in enumerate(sources):
        pending = sum(planned[index + 1:])
        state = {'documents_total': len(sources), 'documents_completed': index,
                 'documents_reused': reused_documents, 'document': document['document_id']}

        def report(update):
            update = dict(update)
            for key in update.keys() & {
                'sections_total', 'sections_completed', 'sections_reused', 'sections_split', 'section_retries',
            }:
                update[key] += completed[key]
                if key == 'sections_total':
                    update[key] += pending
            progress({**state, **update})

        report({'stage': 'extracting_sections', 'sections_total': planned[index]})
        key = sha256(canonical([REVISION, runtime.signature, document]))
        path = cache / f'{key}.json'
        if path.is_file():
            current = document_records(path, document)
            count = len(current)
            stats = {'sections_total': count, 'sections_completed': count, 'sections_reused': count,
                     'sections_split': count - planned[index], 'section_retries': 0}
            reused_documents += 1
        else:
            current, stats = extract_sections(runtime, [document], directory, report,
                                              previous_signatures=previous_signatures)
            atomic_json(path, current)

        report({'stage': 'reconciling_document', **stats, 'section': None})
        graph.add_document(current)
        atomic_json(updates / f'{document["document_id"]}.json', {
            'document_id': document['document_id'], 'cache_key': key,
            'sections_completed': len(current),
            'aliases': [[list(alias), list(graph.resolve(alias)), section['page'], quote]
                        for alias, section, quote in graph.evidence
                        if section['document_id'] == document['document_id']],
        })
        records.extend(current)
        completed.update(stats)
        progress({**state, **completed, 'sections_total': completed['sections_total'] + pending,
                  'documents_completed': index + 1, 'documents_reused': reused_documents,
                  'reconciled_aliases': len(graph.aliases)})

    progress({'stage': 'compiling', 'section': None, 'document': None})
    xml, counts = compile_owl(iri, records, concepts=graph)
    return xml, {'prompt_revision': REVISION, **completed, **counts,
                 'documents_completed': len(sources), 'documents_reused': reused_documents,
                 'reconciled_aliases': len(graph.aliases)}
