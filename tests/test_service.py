import asyncio
import io
import json
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from supernova import PROTOCOL
from supernova.api import Application
from supernova.artifacts import normalize
from supernova.contracts import ServiceError, canonical, sha256, validate_bundle
from supernova.documents import extract
from supernova.runtime import NativeRuntime, messages
from supernova.store import Store
from supernova.worker import run_once

TOKEN = 'test-'+'a'*40


def pdf_bytes(text='ONLY ORIGINAL PDF CONTENT'):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    stream = DecodedStreamObject()
    stream.set_data(('BT /F1 12 Tf 30 700 Td ('+text+') Tj ET').encode())
    page[NameObject('/Contents')] = stream
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})})})
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


def package(content=None, build_id=None):
    content = pdf_bytes() if content is None else content
    doc_id = str(uuid.uuid4())
    manifest = {'protocol': PROTOCOL, 'build_id': build_id or str(uuid.uuid4()),
                'snapshot_id': str(uuid.uuid4()), 'documents': [
                    {'id': doc_id, 'filename': 'manual.pdf', 'path': f'files/{doc_id}.pdf',
                     'sha256': sha256(content)}]}
    return packed(manifest, {manifest['documents'][0]['path']: content}), manifest


def packed(manifest, files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('manifest.json', canonical(manifest))
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def ontology_xml(iri):
    return f'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
xmlns:owl="http://www.w3.org/2002/07/owl#"><owl:Ontology rdf:about="{iri}"/>
<owl:Class rdf:about="{iri}#Entita"/></rdf:RDF>'''


def test_files_are_independently_read_and_hash_verified():
    data, manifest = package()
    _, sources = extract(data)
    assert 'ONLY ORIGINAL PDF CONTENT' in sources[0]['pages'][0]['text']
    assert sources[0]['sha256'] == manifest['documents'][0]['sha256']
    path = manifest['documents'][0]['path']
    with pytest.raises(ServiceError, match='hash_mismatch'):
        validate_bundle(packed(manifest, {path: b'changed'}))
    with pytest.raises(ServiceError, match='unexpected_bundle_members'):
        validate_bundle(packed(manifest, {path: pdf_bytes(), '../outside': b'bad'}))
    manifest['documents'][0]['text'] = 'Sirio extraction must not be accepted'
    with pytest.raises(ServiceError, match='invalid_document_manifest'):
        validate_bundle(packed(manifest, {path: pdf_bytes()}))


def test_idempotency_and_worker_recovery(tmp_path):
    store = Store(tmp_path)
    data, manifest = package()
    key = manifest['build_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.submit(data, key), range(4)))
    assert all(r['id'] == key for r in results)
    assert store.claim()['id'] == key
    assert store.claim() is None
    store.recover_interrupted()
    assert store.get(key)['error'] == 'worker_interrupted'
    assert store.claim() is None  # never reruns uncertain GPU generation
    other, changed = package(build_id=key)
    with pytest.raises(ServiceError, match='idempotency_conflict'):
        store.submit(other, key)


def test_authenticated_api_and_artifact_state(tmp_path):
    store = Store(tmp_path)
    app = Application(store, TOKEN, ready=lambda: True)
    data, manifest = package()
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://service') as client:
            assert (await client.post('/v1/ontology-jobs', content=data)).status_code == 401
            headers = {'Authorization': 'Bearer '+TOKEN, 'Content-Type': 'application/zip',
                       'Idempotency-Key': manifest['build_id']}
            response = await client.post('/v1/ontology-jobs', content=data, headers=headers)
            assert response.status_code == 202
            assert (await client.get('/v1/ontology-jobs/'+manifest['build_id']+'/artifact', headers=headers)).status_code == 409
            assert (await client.get('/health/ready', headers=headers)).status_code == 200
            assert (await client.post('/v1/ontology-jobs/'+manifest['build_id']+'/cancel', headers=headers)).json()['status'] == 'cancelled'
    asyncio.run(scenario())


@pytest.mark.parametrize('bad_entity', [False, True])
def test_worker_native_stream_preserves_output_and_rejects_invalid_xml(tmp_path, bad_entity):
    data, manifest = package()
    store = Store(tmp_path)
    store.submit(data, manifest['build_id'])
    iri = 'urn:sinapsia:ontology:'+manifest['build_id']
    xml = ontology_xml(iri)
    if bad_entity:
        xml = xml.replace('rdf:about="' + iri + '#Entita"', 'rdf:about="&xsd;string"')
    calls = []
    def transport(request):
        body = json.loads(request.content)
        if request.url.path == '/tokenize':
            assert 'ONLY ORIGINAL PDF CONTENT' in body['content']
            return httpx.Response(200, json={'tokens': [1, 2, 3]})
        calls.append(body)
        assert body['ignore_eos'] is False and body['n_predict'] == -1
        assert body['stop'] == []
        assert body['grammar'].startswith('root ::= reasoning')
        events = [{'content': 'Thinking mentions </rdf:RDF> but is not the answer.'},
                  {'content': '\n</think>\n'+xml.removesuffix('</rdf:RDF>')},
                  {'content': '</rdf:'}, {'content': 'RDF>'},
                  {'stop': True, 'stop_type': 'eos',
                   'truncated': False, 'generation_settings': {'grammar': body['grammar']}}]
        return httpx.Response(200, content=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
    runtime = NativeRuntime('http://runner', transport=httpx.MockTransport(transport), model_digest='test')
    assert run_once(store, runtime)
    result = store.get(manifest['build_id'])
    directory = store.directory(manifest['build_id'])
    assert (directory/'original.owl').read_text() == xml
    assert (directory/'native-final.json').exists()
    assert not run_once(store, runtime) and len(calls) == 1
    if bad_entity:
        assert (result['status'], result['error']) == ('failed', 'invalid_xml')
        assert not (directory/'ontology.owl').exists()
        return
    assert result['status'] == 'completed'
    assert result['report']['logical_check'] == 'not_run'
    assert result['report']['visual_content_processed'] is False
    assert (directory/'ontology.owl').read_text() == xml
    assert sha256((directory/'ontology.owl').read_bytes()) == result['artifact_sha256']


def test_native_missing_eos_override_is_not_claimed_complete(tmp_path):
    iri = 'urn:test:supernova'
    def transport(request):
        if request.url.path == '/tokenize':
            return httpx.Response(200, json={'tokens': [1]})
        return httpx.Response(200, content='data: '+json.dumps({'stop': True, 'stop_type': 'eos'})+'\n\n')
    runtime = NativeRuntime('http://runner', transport=httpx.MockTransport(transport))
    with pytest.raises(ServiceError, match='generation_incomplete'):
        runtime.generate(messages([], iri), tmp_path, lambda _: None, iri=iri)
    assert (tmp_path/'native-final.json').exists()


def test_context_overflow_never_starts_inference(tmp_path):
    def transport(request):
        assert request.url.path == '/tokenize'
        return httpx.Response(200, json={'tokens': [1]*3000})
    runtime = NativeRuntime('http://runner', context=4096, transport=httpx.MockTransport(transport))
    with pytest.raises(ServiceError, match='context_capacity_exceeded'):
        runtime.generate(messages([], 'urn:test'), tmp_path, lambda _: None, iri='urn:test')


def test_imports_and_entities_are_rejected_and_normalization_is_mechanical():
    iri = 'urn:test:normalization'
    xml = ontology_xml(iri)
    with pytest.raises(ServiceError, match='entities_forbidden'):
        normalize('<!DOCTYPE x>'+xml)
    with pytest.raises(ServiceError, match='imports_forbidden'):
        normalize(xml.replace('<owl:Ontology rdf:about="'+iri+'"/>',
            '<owl:Ontology rdf:about="'+iri+'"><owl:imports rdf:resource="https://outside.example/a"/></owl:Ontology>'))
    malformed = xml.replace('22-rdf-syntax', '22-0-rdf-syntax')
    normalized, report = normalize(malformed, iri)
    assert normalized == xml and report['normalization']['rdf_namespace'] == 1


def test_cancellation_before_submission_prevents_late_generation(tmp_path):
    store = Store(tmp_path)
    data, manifest = package()
    key = manifest['build_id']
    assert store.cancel(key)['status'] == 'cancelled'
    assert store.submit(data, key)['status'] == 'cancelled'
    assert store.claim() is None


def test_single_quote_iris_are_expanded_without_losing_semantics():
    xml = ontology_xml('urn:test:quotes').replace('xmlns:owl=', 'xmlns:pc="urn:test:quotes#" xmlns:owl=')
    xml = xml.replace('rdf:about="urn:test:quotes#Entita"', "rdf:about='pc:Entita'")
    text, report = normalize(xml, 'urn:test:quotes')
    assert "rdf:about='urn:test:quotes#Entita'" in text
    assert report['normalization']['iri_pc'] == 1


def test_mixed_named_subclass_is_translated_without_reversing_or_losing_annotations():
    from rdflib import OWL, RDF, RDFS, Graph, URIRef
    iri = 'urn:test:mixed'
    original = ontology_xml(iri).replace('xmlns:owl=', 'xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#" xmlns:owl=').replace('</rdf:RDF>', '''
      <!-- Preserve this comment. -->
      <owl:SubClassOf>
        <owl:Class rdf:about="urn:test:mixed#Child"/>
        <owl:Class rdf:about="urn:test:mixed#Parent"/>
      </owl:SubClassOf>
    </rdf:RDF>''')
    text, stats = normalize(original, iri)
    graph = Graph().parse(data=text, format='xml')
    child, parent = URIRef(iri+'#Child'), URIRef(iri+'#Parent')
    assert (child, RDFS.subClassOf, parent) in graph
    assert (parent, RDFS.subClassOf, child) not in graph
    assert (child, RDF.type, OWL.Class) in graph and (parent, RDF.type, OWL.Class) in graph
    assert '<!-- Preserve this comment. -->' in text
    assert set(Graph().parse(data=ontology_xml(iri), format='xml')).issubset(set(graph))
    assert stats['normalization']['mixed_subclass_axioms'] == 1
    assert normalize(text, iri)[0] == text  # repair is idempotent


@pytest.mark.parametrize('extra', [
    '<owl:Class rdf:about="urn:test#Child"/><owl:Restriction/>',
    '<owl:Class rdf:about="urn:test#Child"/><owl:Class rdf:about="urn:test#Parent"/><rdfs:comment>note</rdfs:comment>',
    '<owl:Class rdf:about="urn:test#Child"><rdfs:label>child</rdfs:label></owl:Class><owl:Class rdf:about="urn:test#Parent"/>',
])
def test_ambiguous_mixed_axioms_are_rejected_instead_of_dropping_content(extra):
    text = ontology_xml('urn:test').replace('xmlns:owl=', 'xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#" xmlns:owl=')
    text = text.replace('</rdf:RDF>', '<owl:SubClassOf>'+extra+'</owl:SubClassOf></rdf:RDF>')
    with pytest.raises(ServiceError, match='unsupported_mixed_owlxml'):
        normalize(text, 'urn:test')


def test_unasserted_annotation_is_reported_without_inventing_a_triple():
    from rdflib import RDFS, Graph, Literal, URIRef

    text = ontology_xml('urn:test:annotations').replace('</rdf:RDF>', '''
      <owl:Axiom>
        <owl:annotatedSource rdf:resource="urn:test:annotations#Entita"/>
        <owl:annotatedProperty rdf:resource="http://www.w3.org/2000/01/rdf-schema#comment"/>
        <owl:annotatedTarget xml:lang="it">unasserted</owl:annotatedTarget>
      </owl:Axiom></rdf:RDF>''')
    normalized, report = normalize(text, 'urn:test:annotations')
    graph = Graph().parse(data=normalized, format='xml')
    assert report['unasserted_axiom_annotations'] == 1
    assert (URIRef('urn:test:annotations#Entita'), RDFS.comment, Literal('unasserted', lang='it')) not in graph
