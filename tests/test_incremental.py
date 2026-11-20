from rdflib import OWL, RDF, RDFS, SKOS, Graph, Literal, Namespace

from supernova.incremental import build_incremental


def test_document_updates_reuse_extraction_and_keep_rule_scopes(tmp_path):
    iri = 'urn:test:manual'
    ns = Namespace(iri + '#')
    first = 'La Random Access Memory è volatile.'
    second = 'Random Access Memory (RAM).'
    calls = []

    class Runner:
        signature = 'test-model-and-prompt'

        def generate_section(self, section, directory, progress):
            name = 'Random Access Memory' if section['text'] == first else 'RAM'
            calls.append(section['document_id'])
            return {
                'status': 'complete',
                'entities': [
                    {'id': 'memory', 'kind': 'class', 'label': name, 'scope': ''},
                    {'id': 'procedure', 'kind': 'procedure', 'label': 'Verifica', 'scope': section['document_id']},
                    {'id': 'condition', 'kind': 'individual', 'label': 'Condizione', 'scope': section['document_id']},
                ],
                'relations': [
                    {'source': 'procedure', 'kind': 'condition', 'label': '', 'target': 'condition',
                     'quote': section['text']},
                ],
                'attributes': [],
            }

    documents = [
        {'document_id': key, 'filename': key + '.txt', 'pages': [{'number': 1, 'text': text}]}
        for key, text in [('manual', first), ('supplement', second)]
    ]

    def build(name, selected, events):
        folder = tmp_path / name
        folder.mkdir()
        xml, report = build_incremental(Runner(), selected, folder, events.append, iri=iri)
        assert len(list((folder / 'document-updates').glob('*.json'))) == len(selected)
        return Graph().parse(data=xml, format='xml'), report

    original, _ = build('original', documents[:1], [])
    events = []
    graph, result = build('update', documents, events)
    assert calls == ['manual', 'supplement']
    assert result['documents_completed'] == 2 and result['documents_reused'] == 1
    assert result['reconciled_aliases'] == 1
    memory, = graph.subjects(SKOS.altLabel, Literal('RAM'))
    assert (memory, RDF.type, OWL.Class) in graph
    assert (memory, RDFS.label, Literal('Random Access Memory')) in original
    assert len(set(graph.subjects(RDFS.label, Literal('Verifica')))) == 2
    sources = {str(v) for v in graph.objects(None, ns.fonte)}
    assert any(source.startswith('doc=manual; page=1;') for source in sources)
    assert any(source.startswith('doc=supplement; page=1;') for source in sources)
    assert graph.value(next(graph.subjects(OWL.annotatedProperty, SKOS.altLabel)), ns.fonte)
    totals = [event['sections_completed'] for event in events if 'sections_completed' in event]
    assert totals == sorted(totals)
    reduced, _ = build('withdrawal', documents[1:], [])
    assert (None, RDFS.label, Literal('Random Access Memory')) not in reduced
    assert not any(str(v).startswith('doc=manual;') for v in reduced.objects(None, ns.fonte))
    assert calls == ['manual', 'supplement']


def test_cli_preserves_document_ids_when_adding_files(tmp_path):
    from scripts.client import make_bundle
    from supernova.contracts import validate_bundle

    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_text('First document.')
    second.write_text('Second document.')
    one, archive_one = make_bundle([first])
    two, archive_two = make_bundle([second, first])
    manifest_one = validate_bundle(archive_one)
    manifest_two = validate_bundle(archive_two)
    assert one != two
    assert manifest_one['documents'][0]['id'] == manifest_two['documents'][1]['id']
