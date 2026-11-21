import json
import sys
import uuid
from collections import Counter

import pytest
from rdflib import OWL, RDF, Graph, Namespace
from test_service import package, packed, pdf_bytes

from orbitowl.compiler import compile_owl
from orbitowl.contracts import ServiceError, canonical, sha256
from orbitowl.documents import extract
from orbitowl.pipeline import (
    atomic_json,
    build_ontology,
    sections,
    split,
    split_offsets,
)
from orbitowl.runtime import NativeRuntime
from orbitowl.section_schema import (
    LIMITS,
    entity_grammar,
    fact_grammar,
    validate_section,
)
from orbitowl.store import Store
from orbitowl.worker import main, run_once


def structured(quote, scope=''):
    return {
        'status': 'complete',
        'entities': [
            {'id': 'p', 'kind': 'procedure', 'label': 'Riavvio', 'scope': scope},
            {'id': 'r', 'kind': 'individual', 'label': 'Verifica', 'scope': scope},
        ],
        'relations': [
            {'source': 'p', 'kind': 'prerequisite', 'label': '', 'target': 'r', 'quote': quote},
        ],
        'attributes': [],
    }


def test_worker_resumes_accepted_section_and_merges_exact_concepts(tmp_path):
    class WorkerInterrupted(BaseException):
        pass

    class Runner:
        signature = 'fixed-model'

        def __init__(self):
            self.calls = []

        def generate_section(self, section, directory, progress):
            self.calls.append(section['document_id'])
            if len(self.calls) == 2:
                raise WorkerInterrupted
            return structured(section['text'])

    _, manifest = package()
    filenames = ('prima.txt', 'seconda.txt')
    documents = []
    originals = {}
    for name in filenames:
        doc = str(uuid.uuid4())
        path = f'files/{doc}.txt'
        originals[path] = f'Requisito documentato in {name}.'.encode()
        documents.append({'id': doc, 'filename': name, 'path': path, 'sha256': sha256(originals[path])})
    manifest['documents'] = documents
    key = manifest['build_id']
    store = Store(tmp_path)
    store.submit(packed(manifest, originals), key)
    runner = Runner()
    with pytest.raises(WorkerInterrupted):
        run_once(store, runner)
    assert store.get(key)['report']['sections_completed'] == 1
    assert store.get(key)['status'] == 'running'
    store = Store(tmp_path)
    store.recover_interrupted()
    assert store.get(key)['status'] == 'queued'
    assert run_once(store, runner)
    assert runner.calls == [documents[0]['id'], documents[1]['id'], documents[1]['id']]
    result = store.get(key)
    assert result['status'] == 'completed'
    assert result['report']['sections_reused'] == 1
    ns = Namespace(f'urn:sinapsia:ontology:{key}#')
    graph = Graph().parse(store.directory(key)/'ontology.owl', format='xml')
    (subject, target), = graph.subject_objects(ns.richiedePrerequisito)
    assert len(set(graph.subjects(RDF.type, OWL.NamedIndividual))) == 2
    axiom, = graph.subjects(OWL.annotatedSource, subject)
    assert graph.value(axiom, OWL.annotatedTarget) == target
    assert {str(ref).split(';')[0] for ref in graph.objects(axiom, ns.fonte)} == {
        f"doc={doc['id']}" for doc in documents
    }


def test_output_limit_splits_only_the_failed_section_and_keeps_scopes(tmp_path):
    class Runner:
        signature = 'test'

        def __init__(self):
            self.calls = Counter()

        def generate_section(self, section, directory, progress):
            page = section['page']
            self.calls[page] += 1
            if len(section['text']) > 800:
                raise ServiceError('section_output_limit')
            return structured('Controllo documentato', str(page))

    runner = Runner()
    docs = [{'document_id': str(uuid.uuid4()), 'filename': 'testo.txt', 'pages': [
        {'number': 1, 'text': 'Controllo documentato'},
        {'number': 2, 'text': 'Controllo documentato. ' * 49},
    ]}]
    iri = 'urn:test:sections'
    xml, report = build_ontology(runner, docs, tmp_path, lambda update: None, iri=iri)
    assert runner.calls == {1: 1, 2: 3}
    assert report['sections_completed'] == 3 and report['sections_split'] == 1
    graph = Graph().parse(data=xml, format='xml')
    ns = Namespace(iri + '#')
    # Entities unify across sections while retaining every pc:ambito annotation.
    assert len(set(graph.subjects(RDF.type, OWL.NamedIndividual))) == 2
    assert {str(v) for v in graph.objects(None, ns.ambito)} == {'1', '2'}
    again = build_ontology(runner, docs, tmp_path, lambda update: None, iri=iri)[1]
    assert again['sections_reused'] == 3 and runner.calls == {1: 1, 2: 3}


def test_class_attribute_retains_qualified_source_without_a_datatype_assertion():
    quote = 'Un HDD desktop è spesso da 3,5 pollici.'
    data = {
        'status': 'complete',
        'entities': [{'id': 'a1', 'kind': 'class', 'label': 'HDD desktop', 'scope': ''}],
        'relations': [],
        'attributes': [{'source': 'a1', 'label': 'dimensione', 'type': 'text',
                        'value': '3,5 pollici', 'quote': quote}],
    }
    validate_section(data, quote)
    iri = 'urn:test:class-attribute'
    xml, _ = compile_owl(iri, [({'document_id': 'document', 'page': 2}, data)])
    graph = Graph().parse(data=xml, format='xml')
    subject, = graph.subjects(RDF.type, OWL.Class)
    prop, = (p for p, o in graph.predicate_objects(subject) if str(o) == quote)
    assert (prop, RDF.type, OWL.AnnotationProperty) in graph
    assert not list(graph.subjects(RDF.type, OWL.DatatypeProperty))
    axiom, = graph.subjects(OWL.annotatedSource, subject)
    assert str(graph.value(axiom, Namespace(iri+'#').fonte)) == f'doc=document; page=2; quote={quote}'

    data['attributes'][0]['source'] = 'unknown'
    with pytest.raises(ServiceError, match='section_attribute_entity_unknown'):
        validate_section(data, quote)


def test_saturated_558_character_section_can_split_beyond_two_levels(tmp_path):
    class Runner:
        signature = 'test'

        def generate_section(self, section, directory, progress):
            text = section['text']
            if len(text) > 100:
                return {
                    'status': 'complete',
                    'entities': [
                        {'id': f'a{i}', 'kind': 'class', 'label': f'Entità {i}', 'scope': ''}
                        for i in range(LIMITS['entities'])
                    ],
                    'relations': [],
                    'attributes': [],
                }
            return structured(text.strip())

    text = ('Prima del riavvio, controlla che i collegamenti siano integri. ' * 12)[:558]
    sources = [{'document_id': 'manuale', 'filename': 'test.txt', 'pages': [{'number': 2, 'text': text}]}]
    events = []
    _, report = build_ontology(Runner(), sources, tmp_path, events.append, iri='urn:test:density')
    assert report['sections_completed'] > 4
    assert report['sections_completed'] == report['sections_total']
    diagnostics = [event['last_split'] for event in events if 'last_split' in event]
    assert all(x['error'] == 'section_capacity_reached' for x in diagnostics)
    assert all(x['full_lists'] == {'entities': LIMITS['entities']} for x in diagnostics)


def test_saturation_has_a_durable_attempt_budget(tmp_path, monkeypatch):
    class Runner:
        signature = 'test'

        def __init__(self):
            self.calls = 0

        def generate_section(self, section, directory, progress):
            self.calls += 1
            raise ServiceError('section_capacity_reached', details={'full_lists': {'entities': 14}})

    monkeypatch.setattr('orbitowl.pipeline.MAX_SECTION_ATTEMPTS', 2)
    runner = Runner()
    events = []
    text = 'Prima del riavvio controlla i collegamenti. ' * 13
    sources = [{'document_id': 'manuale', 'filename': 'test.txt', 'pages': [{'number': 1, 'text': text}]}]
    for _ in range(2):
        with pytest.raises(ServiceError, match='section_attempt_budget_exhausted'):
            build_ontology(runner, sources, tmp_path, events.append, iri='urn:test:budget')
        assert events[-1]['failed_section']['attempts'] == 2
    assert runner.calls == 2
    assert events[1]['last_split']['error'] == 'section_capacity_reached'
    assert events[1]['last_split']['full_lists'] == {'entities': 14}


def test_model_split_response_is_a_contract_error_and_cannot_fan_out(tmp_path):
    class Runner:
        signature = 'test'

        def __init__(self):
            self.calls = 0

        def generate_section(self, section, directory, progress):
            self.calls += 1
            return {'status': 'split'}

    runner = Runner()
    text = 'Verifica i collegamenti. ' * 40
    sources = [{'document_id': 'manuale', 'filename': 'test.txt', 'pages': [{'number': 1, 'text': text}]}]
    events = []
    with pytest.raises(ServiceError, match='section_output_contract_violation'):
        build_ontology(runner, sources, tmp_path, events.append, iri='urn:test:output-contract')
    assert runner.calls == 1
    assert not any('last_split' in event for event in events)


@pytest.mark.parametrize('invalid_kind', [True, False])
def test_invalid_fact_is_rejected_without_splitting(tmp_path, invalid_kind):
    text = 'Riavvio dopo la verifica. La procedura termina al completamento della verifica.'
    rejected = structured('Riavvio dopo la verifica.' if invalid_kind else 'Citazione estranea.')
    if invalid_kind:
        rejected['entities'][0]['kind'] = 'individual'
    code = 'section_relation_types_invalid' if invalid_kind else 'section_quote_not_found'

    class Runner:
        signature = 'test'

        def generate_section(self, section, directory, progress):
            return rejected

    sources = [{'document_id': 'doc', 'filename': 'test.txt', 'pages': [{'number': 1, 'text': text}]}]
    events = []
    with pytest.raises(ServiceError, match=code):
        build_ontology(Runner(), sources, tmp_path, events.append, iri='urn:test:invalid-fact')
    assert not any('last_split' in event for event in events)
    assert not list((tmp_path/'sections').glob('*/validated.json'))


@pytest.mark.parametrize(('previous_revision', 'failure'), [
    ('structured-sections-1.2', 'section_attempt_budget_exhausted'),
    ('structured-sections-1.3', 'section_attempt_budget_exhausted'),
    ('structured-sections-1.4', 'section_attempt_budget_exhausted'),
    ('structured-sections-1.6.1', 'section_quote_not_found'),
    ('structured-sections-1.6.2', 'section_attempt_budget_exhausted'),
])
def test_resumes_job_and_revalidates_legacy_checkpoints(tmp_path, monkeypatch, capsys, previous_revision, failure):
    text = 'Riavvio dopo la verifica. Completa la verifica prima del riavvio.'
    archive, manifest = package(content=pdf_bytes(text))
    key = manifest['build_id']
    store = Store(tmp_path)
    store.submit(archive, key)
    _, sources = extract(archive)
    (initial,) = sections(sources)
    offsets = split_offsets(initial['text'])
    left, right = split(initial, offsets)
    runtime = NativeRuntime('http://runner')
    checkpoints = store.directory(key)/'sections'

    def legacy_path(section):
        return checkpoints/sha256(canonical([runtime.previous_signatures[previous_revision], section]))

    initial_dir, left_dir, right_dir = map(legacy_path, (initial, left, right))
    for directory in (initial_dir, left_dir, right_dir):
        directory.mkdir(parents=True)
    atomic_json(initial_dir/'split.json', {'offsets': offsets, 'trigger': 'section_quote_not_found'})
    atomic_json(checkpoints/f'{initial_dir.name}.attempts.json', 32)
    atomic_json(left_dir/'validated.json', structured(left['text']))
    atomic_json(right_dir/'validated.json', structured('citazione estranea al testo'))
    previous_report = {'pipeline_revision': previous_revision, 'sections_completed': 2}
    store.update(key, status='failed', error=failure, report=previous_report)
    monkeypatch.setenv('ORBITOWL_DATA', str(tmp_path))
    monkeypatch.setenv('ORBITOWL_MODEL', runtime.model)
    monkeypatch.setenv('ORBITOWL_CONTEXT', str(runtime.context))
    monkeypatch.setattr(sys, 'argv', ['worker', '--check-resume', key])
    main()
    preview = json.loads(capsys.readouterr().out)
    assert preview['sections_expected'] == 2
    assert preview['sections_reusable'] == 1
    assert store.get(key)['status'] == 'failed'
    monkeypatch.setattr(sys, 'argv', ['worker', '--resume', key])
    with pytest.raises(SystemExit, match='1'):
        main()
    assert json.loads(capsys.readouterr().err)['error'] == 'job_resume_checkpoint_mismatch'
    assert store.get(key)['status'] == 'failed'

    store.update(key, report={**previous_report, 'sections_completed': 1})
    main()
    assert json.loads(capsys.readouterr().out)['status'] == 'queued'

    calls = []

    def generate(section, directory, progress):
        calls.append(section)
        return structured(section['text'])

    monkeypatch.setattr(runtime, 'generate_section', generate)
    with pytest.raises(ServiceError, match='job_resume_not_applicable'):
        store.resume_section_budget_failure(key)
    assert run_once(store, runtime)
    assert calls == [right]
    result = store.get(key)
    assert result['status'] == 'completed'
    assert result['report']['sections_reused'] == 1
    assert result['report']['sections_completed'] == 2


def test_quarantine_skips_one_bad_section_and_completes_corpus(tmp_path):
    class Runner:
        signature = 'quarantine-test'

        def generate_section(self, section, directory, progress):
            if section['page'] == 2:
                raise ServiceError('section_attribute_value_invalid')
            return structured(section['text'])

    sources = [{
        'document_id': str(uuid.uuid4()),
        'filename': 'manual.txt',
        'pages': [
            {'number': 1, 'text': 'Controllo pagina uno.'},
            {'number': 2, 'text': 'Tabella corrotta pagina due.'},
            {'number': 3, 'text': 'Controllo pagina tre.'},
            {'number': 4, 'text': 'Controllo pagina quattro.'},
        ],
    }]
    xml, report = build_ontology(Runner(), sources, tmp_path, lambda _: None, iri='urn:test:quarantine')
    assert report['sections_completed'] == 3
    assert report['sections_skipped'] == 1
    assert (tmp_path / 'skipped-sections.json').is_file()
    graph = Graph().parse(data=xml, format='xml')
    assert len(set(graph.subjects(RDF.type, OWL.NamedIndividual))) == 2


def test_operational_relations_on_classes_and_typed_attribute_grammar():
    quote = 'Ogni intervento hardware richiede un bracciale ESD.'
    data = {
        'status': 'complete',
        'entities': [
            {'id': 'c1', 'kind': 'class', 'label': 'Intervento Hardware', 'scope': ''},
            {'id': 'c2', 'kind': 'class', 'label': 'Bracciale ESD', 'scope': ''},
            {'id': 'p1', 'kind': 'procedure', 'label': 'Sostituzione SSD', 'scope': ''},
        ],
        'relations': [
            {'source': 'c1', 'kind': 'prerequisite', 'label': '', 'target': 'c2', 'quote': quote},
            {'source': 'p1', 'kind': 'prerequisite', 'label': '', 'target': 'c2', 'quote': quote},
        ],
        'attributes': [],
    }
    validate_section(data, quote)
    iri = 'urn:test:class-ops'
    xml, counts = compile_owl(iri, [({'document_id': 'doc1', 'page': 1}, data)])
    assert counts['operational_relations'] == 2
    graph = Graph().parse(data=xml, format='xml')
    assert len(set(graph.subjects(RDF.type, OWL.Restriction))) == 2
    grammar = fact_grammar(data['entities'], {'p1': quote})
    assert 'int-val' in grammar and 'dec-val' in grammar and 'bool-val' in grammar
    assert 'short-nonempty' in grammar
    # llama.cpp GBNF rule names only allow [a-zA-Z0-9-] (underscores cause HTTP 400).
    import re
    for g in (entity_grammar(), grammar):
        for line in g.splitlines():
            name, _, rhs = line.partition(' ::= ')
            assert re.fullmatch(r'[a-zA-Z0-9-]+', name), f'Invalid GBNF rule name: {name}'
            assert rhs.strip(), f'Empty GBNF rule: {name}'
            # Strip quoted literals and character classes; ensure no non-terminal contains '_'.
            stripped = re.sub(r'"(?:\\.|[^"\\])*"', ' ', rhs)
            stripped = re.sub(r'\[(?:\\.|[^\]\\])*\]', ' ', stripped)
            assert '_' not in stripped, f'Underscore in GBNF non-terminal: {line}'


def test_pdf_with_blank_page_is_extracted_instead_of_failing():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)  # Blank cover / diagram page
    page2 = writer.add_blank_page(width=600, height=800)
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 30 700 Td (CONTENUTO PAGINA DUE) Tj ET')
    page2[NameObject('/Contents')] = stream
    page2[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})})})
    import io
    buf = io.BytesIO()
    writer.write(buf)
    archive, _ = package(content=buf.getvalue())
    _, sources = extract(archive)
    assert sources[0]['empty_pages'] == 1
    assert len(sources[0]['pages']) == 1
    assert sources[0]['pages'][0]['number'] == 2
    assert 'CONTENUTO PAGINA DUE' in sources[0]['pages'][0]['text']


def test_unresolved_triggers_one_shot_entity_recovery_before_splitting(tmp_path):
    import httpx

    calls = []

    def transport(request):
        body = json.loads(request.content)
        if request.url.path == '/tokenize':
            return httpx.Response(200, json={'tokens': [1, 2, 3]})
        calls.append(body)
        if len(calls) == 1:
            # Initial entities miss the prerequisite entity.
            text = json.dumps({'entities': [
                {'id': 'p1', 'kind': 'procedure', 'label': 'Riavvio', 'scope': ''},
            ]})
        elif len(calls) == 2:
            # First facts call flags p1 as unresolved.
            text = json.dumps({'relations': [], 'attributes': [], 'unresolved': ['p1']})
        elif len(calls) == 3:
            # Recovery call extracts the missing entity.
            text = json.dumps({'entities': [
                {'id': 'r1', 'kind': 'individual', 'label': 'Verifica', 'scope': ''},
            ]})
        else:
            # Retry facts succeeds cleanly!
            text = json.dumps({
                'relations': [{'source': 'p1', 'kind': 'prerequisite', 'label': '', 'target': 'r1', 'quote': 'p1'}],
                'attributes': [],
                'unresolved': [],
            })
        events = [
            {'content': text},
            {'stop': True, 'stop_type': 'eos', 'truncated': False,
             'generation_settings': {'grammar': body['grammar']}},
        ]
        return httpx.Response(200, content=''.join('data: ' + json.dumps(e) + '\n\n' for e in events))

    runtime = NativeRuntime('http://runner', transport=httpx.MockTransport(transport))
    data = runtime.generate_section({'text': 'Prima del riavvio eseguire la verifica.'}, tmp_path, lambda _: None)
    assert len(calls) == 4
    assert len(data['entities']) == 2
    assert len(data['relations']) == 1

