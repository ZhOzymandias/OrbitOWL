import uuid
from collections import Counter

import pytest
from rdflib import OWL, RDF, Graph, Namespace
from test_service import package, packed

from supernova.compiler import compile_owl
from supernova.contracts import ServiceError, sha256
from supernova.pipeline import build_ontology
from supernova.section_schema import validate_section
from supernova.store import Store
from supernova.worker import run_once


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
    assert len(set(graph.subjects(RDF.type, OWL.NamedIndividual))) == 4
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
